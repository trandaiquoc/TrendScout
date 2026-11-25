"""Offline transactional contract tests for Firestore-backed shared cache blobs."""

import asyncio
import hashlib
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app.collections.service import CollectionService
from app.google_live_settings import GoogleLiveSettings
from app.jobs.public_admission import AdmissionDenied, AdmissionPolicy, PoolLimit, PublicAdmission
from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant
from app.main import create_app, create_normal_google_live_app
from app.orchestration.firestore_shared_live_composition import (
    PRODUCTION_FIRESTORE_PROJECT_ID,
    create_firestore_shared_live_composition,
)
from app.orchestration.normal_google_live_composition import (
    _shared_google_candidate_rows,
    normal_google_live_composition,
)
from app.orchestration.vercel_collection_read_composition import (
    vercel_collection_read_composition,
)
from app.orchestration.vercel_wikimedia_live_composition import (
    vercel_shared_live_composition,
    vercel_wikimedia_live_composition,
)
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
)
from app.sources.google_trends.handler import GoogleTrendsDiscoveryHandler
from app.sources.google_trends.trending_now import TRENDING_NOW_PARSER_VERSION
from app.sources.guarded_collector import CollectionLimits
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
from app.storage.firestore_shared_cache import create_firestore_shared_cache
from app.storage.firestore_snapshot_blobs import FirestoreSnapshotBlobs, _SnapshotReadCache
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
SCOPE = CacheScope(GOOGLE_SOURCE_ID, "serpapi_google", "linen pants", "a" * 64,
                   "gl=us", "en", "7d", "v1", "policy-v1")
GRANT = CacheGrant(GOOGLE_SOURCE_ID, "policy-v1", NOW + timedelta(days=1),
                   max_fresh_seconds=10, max_stale_seconds=20,
                   max_retention_seconds=30, allowed=True)


class FakeSnapshot:
    def __init__(self, value, doc_id=None):
        self.value = deepcopy(value)
        self.exists = value is not None
        self.id = doc_id

    def to_dict(self):
        return deepcopy(self.value)


class FakeDocument:
    def __init__(self, client, path):
        self.client, self.path = client, path

    def collection(self, name):
        return FakeCollection(self.client, f"{self.path}/{name}")

    def get(self):
        self.client.document_gets += 1
        return FakeSnapshot(self.client.data.get(self.path))


class FakeCollection:
    def __init__(self, client, path):
        self.client, self.path = client, path

    def document(self, key):
        return FakeDocument(self.client, f"{self.path}/{key}")

    def where(self, field, operator, expected):
        return FakeQuery(self, field, operator, expected)


class FakeQuery:
    def __init__(self, collection, field, operator, expected):
        self.collection, self.field, self.operator, self.expected = (
            collection, field, operator, expected
        )

    def stream(self):
        prefix = f"{self.collection.path}/"
        result = []
        for path, value in self.collection.client.data.items():
            if not path.startswith(prefix):
                continue
            doc_id = path[len(prefix):]
            if "/" in doc_id or self.operator != "<=":
                continue
            if value.get(self.field, "") <= self.expected:
                result.append(FakeSnapshot(value, doc_id))
        return iter(result)


class FakeTransaction:
    def __init__(self, client):
        self.client, self.pending = client, []

    def get(self, reference):
        return iter([FakeSnapshot(self.client.data.get(reference.path))])

    def set(self, reference, value):
        self.client.set_calls += 1
        if self.client.fail_write_at is not None and len(self.pending) == self.client.fail_write_at:
            raise RuntimeError("injected transaction failure")
        self.pending.append(("set", reference.path, deepcopy(value)))

    def update(self, reference, values):
        self.client.update_calls += 1
        self.pending.append(("update", reference.path, deepcopy(values)))

    def delete(self, reference):
        self.pending.append(("delete", reference.path, None))


class FakeClient:
    def __init__(self):
        self.data, self.fail_write_at = {}, None
        self.document_gets = 0
        self.batch_gets = 0
        self.transaction_calls = 0
        self.set_calls = 0
        self.update_calls = 0

    def collection(self, name):
        return FakeCollection(self, name)

    def transaction(self, max_attempts):
        self.transaction_calls += 1
        return FakeTransaction(self)

    def get_all(self, references):
        self.batch_gets += 1
        snapshots = [
            FakeSnapshot(self.data.get(reference.path), reference.path.rsplit("/", 1)[-1])
            for reference in references
        ]
        return iter(reversed(snapshots))


class SharedRuntimeFakeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.project = PRODUCTION_FIRESTORE_PROJECT_ID
        self._database = "(default)"


class _LiveMockWikimediaGateway(WikimediaTopPageGateway):
    @property
    def data_mode(self):
        return "LIVE"

    async def fetch_day(self, **kwargs):
        return replace(await super().fetch_day(**kwargs), data_mode="LIVE")


def fake_transactional(callback):
    def run(transaction):
        result = callback(transaction)
        updated = deepcopy(transaction.client.data)
        for action, path, value in transaction.pending:
            if action == "set":
                updated[path] = value
            elif action == "update":
                if path not in updated:
                    raise RuntimeError("cannot update a missing document")
                updated[path].update(value)
            else:
                updated.pop(path, None)
        transaction.client.data = updated
        return result
    return run


class FirestoreSharedCacheTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.atomic = FirestoreAtomicStateStore(self.client)
        self.blobs = FirestoreSnapshotBlobs(self.client)
        self.patchers = [
            patch("app.storage.firestore_atomic_state.firestore.transactional", fake_transactional),
            patch("app.storage.firestore_snapshot_blobs.firestore.transactional", fake_transactional),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def test_large_snapshot_round_trip_duplicate_and_expiry(self):
        records = [{"text": "x" * 500_000, "record_count": 1}]
        ref = self.blobs.put(records, now=NOW, retain_until=NOW + timedelta(seconds=30))
        reopened = FirestoreSnapshotBlobs(self.client)
        self.assertEqual(reopened.get(ref, now=NOW + timedelta(seconds=5)), records)
        set_calls = self.client.set_calls
        self.blobs.put(records, now=NOW, retain_until=NOW + timedelta(seconds=30))
        self.assertEqual(self.client.set_calls, set_calls)
        update_calls = self.client.update_calls
        self.assertEqual(
            self.blobs.put(records, now=NOW, retain_until=NOW + timedelta(seconds=40)), ref
        )
        self.assertGreater(self.client.update_calls, update_calls)
        self.assertEqual(self.client.set_calls, set_calls)
        prefix = f"_ts_d5_cache_blobs/{ref['digest']}/chunks/"
        chunk_retention = [value["retain_until"] for path, value in self.client.data.items()
                           if path.startswith(prefix)]
        self.assertTrue(chunk_retention)
        self.assertTrue(all(value == (NOW + timedelta(seconds=40)).isoformat()
                            for value in chunk_retention))
        self.assertEqual(reopened.get(ref, now=NOW + timedelta(seconds=35)), records)
        with self.assertRaisesRegex(ValueError, "expired"):
            reopened.get(ref, now=NOW + timedelta(seconds=40))

    def test_binary_chunks_round_trip_expire_and_detect_corruption(self):
        records = [{"encoding": "sqlite-gzip-base64-v1", "payload": "a" * 500_000}]
        ref = self.blobs.put(records, now=NOW, retain_until=NOW + timedelta(seconds=30),
                             binary_chunks=True)
        manifest_path = f"_ts_d5_cache_blobs/{ref['digest']}"
        self.assertEqual(self.client.data[manifest_path]["chunk_encoding"], "bytes")
        chunks = [value for path, value in self.client.data.items()
                  if path.startswith(manifest_path + "/chunks/")]
        self.assertTrue(all(type(item["data_bytes"]) is bytes and "data_b64" not in item
                            for item in chunks))
        self.assertEqual(self.blobs.get(ref, now=NOW), records)
        path = manifest_path + "/chunks/00000000"
        self.client.data[path]["data_bytes"] = b"corrupted"
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.blobs.get(ref, now=NOW)
        self.blobs.put(records, now=NOW, retain_until=NOW + timedelta(seconds=30),
                       binary_chunks=True)
        with self.assertRaisesRegex(ValueError, "expired"):
            self.blobs.get(ref, now=NOW + timedelta(seconds=30))
        self.assertEqual(self.blobs.purge_expired(now=NOW + timedelta(seconds=30)), 1)

    def test_binary_chunk_survives_firestore_protobuf_serialization(self):
        from google.cloud.firestore_v1._helpers import decode_dict, encode_dict
        from google.cloud.firestore_v1.types import Document

        ref = self.blobs.put([{"payload": "fixture" * 1000}], now=NOW,
                             retain_until=NOW + timedelta(seconds=30), binary_chunks=True)
        path = f"_ts_d5_cache_blobs/{ref['digest']}/chunks/00000000"
        chunk = self.client.data[path]
        document = Document(fields=encode_dict(chunk))
        decoded = Document.deserialize(Document.serialize(document))
        restored = decode_dict(decoded.fields, client=None)
        self.assertEqual(restored, chunk)
        self.assertIs(type(restored["data_bytes"]), bytes)

    def test_existing_legacy_blob_keeps_its_encoding_on_binary_put(self):
        records = [{"payload": "legacy"}]
        ref = self.blobs.put(records, now=NOW, retain_until=NOW + timedelta(seconds=30))
        same = self.blobs.put(records, now=NOW, retain_until=NOW + timedelta(seconds=40),
                              binary_chunks=True)
        self.assertEqual(ref, same)
        path = f"_ts_d5_cache_blobs/{ref['digest']}/chunks/00000000"
        self.assertIn("data_b64", self.client.data[path])
        self.assertNotIn("data_bytes", self.client.data[path])
        self.assertEqual(self.blobs.get(ref, now=NOW + timedelta(seconds=35)), records)

    def test_collection_snapshot_binary_write_restores_and_failure_keeps_pointer(self):
        import random
        import sqlite3
        from contextlib import closing

        from app.collections.firestore_snapshot import (
            CollectionSnapshotUnavailable,
            FirestoreCollectionSnapshot,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.sqlite"
            writer = FirestoreCollectionSnapshot(self.client, path, utcnow=lambda: NOW)
            self.assertEqual(writer.load(), 0)
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE records(value BLOB)")
                payload = random.Random(9).randbytes(300_000)
                db.execute("INSERT INTO records VALUES(?)", (payload,))
                db.commit()
            self.assertEqual(writer.save(), 1)
            pointer = deepcopy(self.client.data)
            self.assertTrue(any(value.get("chunk_encoding") == "bytes"
                                for value in self.client.data.values()))
            restored_path = Path(directory) / "restored.sqlite"
            reader = FirestoreCollectionSnapshot(self.client, restored_path, utcnow=lambda: NOW)
            self.assertEqual(reader.load(), 1)
            with closing(sqlite3.connect(restored_path)) as db:
                self.assertEqual(db.execute("SELECT value FROM records").fetchone()[0], payload)
                db.execute("INSERT INTO records VALUES(?)", (b"new data",))
                db.commit()
            self.client.fail_write_at = 1
            with self.assertRaises(CollectionSnapshotUnavailable):
                reader.save()
            self.assertEqual(self.client.data, pointer)

    def test_chunk_reads_use_one_order_independent_batch_get(self):
        records = [{"text": "x" * 500_000}]
        reference = self.blobs.put(
            records, now=NOW, retain_until=NOW + timedelta(seconds=30)
        )

        self.assertEqual(self.blobs.get(reference, now=NOW), records)
        self.assertEqual(self.client.batch_gets, 1)
        # The manifest is read once; chunk documents use one batch read.
        self.assertEqual(self.client.document_gets, 1)

    def test_verified_warm_read_cache_skips_chunk_batch_and_still_checks_expiry(self):
        records = [{"text": "x" * 500_000}]
        reference = self.blobs.put(
            records, now=NOW, retain_until=NOW + timedelta(seconds=30)
        )
        read_cache = _SnapshotReadCache()
        first_reader = FirestoreSnapshotBlobs(self.client, read_cache=read_cache)
        with self.assertLogs("app.storage.firestore_snapshot_blobs", level="INFO") as first:
            self.assertEqual(first_reader.get(reference, now=NOW), records)
        self.assertIn("cache=MISS", first.output[0])
        self.assertEqual(self.client.batch_gets, 1)
        self.assertEqual(self.client.document_gets, 1)

        warm_reader = FirestoreSnapshotBlobs(self.client, read_cache=read_cache)
        with self.assertLogs("app.storage.firestore_snapshot_blobs", level="INFO") as warm:
            self.assertEqual(warm_reader.get(reference, now=NOW + timedelta(seconds=5)), records)
        self.assertIn("cache=HIT", warm.output[0])
        self.assertEqual(self.client.batch_gets, 1)
        self.assertEqual(self.client.document_gets, 1)

        with self.assertRaisesRegex(ValueError, "expired"):
            warm_reader.get(reference, now=NOW + timedelta(seconds=30))
        self.assertEqual(self.client.document_gets, 2)
        self.assertIsNone(read_cache.get(
            reference["digest"], raw_bytes=reference["raw_bytes"],
            item_count=reference["item_count"], now=NOW + timedelta(seconds=30),
        ))

    def test_verified_read_cache_rechecks_manifest_after_short_ttl(self):
        records = [{"text": "cached"}]
        reference = self.blobs.put(
            records, now=NOW, retain_until=NOW + timedelta(minutes=5)
        )
        read_cache = _SnapshotReadCache()
        reader = FirestoreSnapshotBlobs(self.client, read_cache=read_cache)

        self.assertEqual(reader.get(reference, now=NOW), records)
        self.assertEqual(reader.get(reference, now=NOW + timedelta(seconds=59)), records)
        self.assertEqual(self.client.document_gets, 1)
        self.assertEqual(reader.get(reference, now=NOW + timedelta(seconds=60)), records)
        self.assertEqual(self.client.document_gets, 2)
        self.assertEqual(self.client.batch_gets, 2)

    def test_verified_read_cache_keeps_multiple_segments_with_bounded_eviction(self):
        records = [[{"text": character * 100}] for character in "abc"]
        references = [self.blobs.put(item, now=NOW, retain_until=NOW + timedelta(minutes=5))
                      for item in records]
        size = references[0]["raw_bytes"]
        reader = FirestoreSnapshotBlobs(self.client, read_cache=_SnapshotReadCache(max_bytes=2 * size))
        self.assertEqual(reader.get(references[0], now=NOW), records[0])
        self.assertEqual(reader.get(references[1], now=NOW), records[1])
        self.assertEqual(reader.get(references[0], now=NOW), records[0])
        self.assertEqual(self.client.batch_gets, 2)
        self.assertEqual(reader.get(references[2], now=NOW), records[2])
        self.assertEqual(reader.get(references[0], now=NOW), records[0])
        self.assertEqual(self.client.batch_gets, 3)
        self.assertEqual(reader.get(references[1], now=NOW), records[1])
        self.assertEqual(self.client.batch_gets, 4)

    def test_snapshot_within_checkpoint_budget_round_trips_across_multiple_chunks(self):
        records = [{"text": "x" * 4_100_000, "record_count": 1}]
        ref = self.blobs.put(records, now=NOW, retain_until=NOW + timedelta(seconds=30))
        self.assertGreater(ref["raw_bytes"], 4_000_016)
        self.assertEqual(self.blobs.get(ref, now=NOW), records)

    def test_partial_transaction_is_not_visible(self):
        self.client.fail_write_at = 1
        with self.assertRaisesRegex(RuntimeError, "injected transaction failure"):
            self.blobs.put([{"text": "x" * 300_000}], now=NOW,
                           retain_until=NOW + timedelta(seconds=30))
        self.assertEqual(self.client.data, {})

    def test_missing_chunk_corruption_and_invalid_payload_fail_closed(self):
        ref = self.blobs.put([{"title": "cached"}], now=NOW,
                             retain_until=NOW + timedelta(seconds=30))
        chunk_path = f"_ts_d5_cache_blobs/{ref['digest']}/chunks/00000000"
        self.client.data.pop(chunk_path)
        with self.assertRaisesRegex(ValueError, "chunk is missing"):
            self.blobs.get(ref, now=NOW)
        with self.assertRaisesRegex(ValueError, "finite UTF-8 JSON"):
            self.blobs.put([{"value": float("nan")}], now=NOW,
                           retain_until=NOW + timedelta(seconds=30))

    def test_expired_manifests_and_chunks_are_purged_without_touching_live_blobs(self):
        expired = self.blobs.put([{"title": "old"}], now=NOW,
                                 retain_until=NOW + timedelta(seconds=30))
        live = self.blobs.put([{"title": "current"}], now=NOW,
                              retain_until=NOW + timedelta(seconds=90))
        deleted = self.blobs.purge_expired(now=NOW + timedelta(seconds=40))
        self.assertEqual(deleted, 1)
        self.assertNotIn(f"_ts_d5_cache_blobs/{expired['digest']}", self.client.data)
        self.assertEqual(self.blobs.get(live, now=NOW + timedelta(seconds=40)),
                         [{"title": "current"}])

    def test_large_cache_is_cross_instance_and_cache_hit_needs_no_provider_call(self):
        cache_a = create_firestore_shared_cache(self.client, max_inline_bytes=256)
        payload = {"status": "SUCCESS", "data_mode": "LIVE", "record_count": 1,
                   "records": [{"title": "linen pants", "description": "x" * 300_000}]}
        cache_a.put(SCOPE, grant=GRANT, origin="LIVE", payload=payload,
                    fetched_at=NOW, now=NOW)

        cache_b = create_firestore_shared_cache(self.client, max_inline_bytes=256)
        provider_requests = []
        cached = cache_b.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=5))
        if cached["status"] not in {"CACHED", "STALE"}:
            provider_requests.append("would-fetch")
        self.assertEqual(cached["status"], "CACHED")
        self.assertEqual(cached["origin"], "LIVE")
        self.assertEqual(cached["fetched_at"], NOW.isoformat())
        self.assertEqual(cached["payload"], payload)
        self.assertEqual(provider_requests, [])
        self.assertEqual(
            SharedCache(FirestoreAtomicStateStore(self.client)).get(
                SCOPE, grant=GRANT, now=NOW + timedelta(seconds=5)
            )["reason"],
            "INVALID_CACHE_RECORD",
        )

        other_scope = CacheScope(GOOGLE_SOURCE_ID, "serpapi_google", "other query", "a" * 64,
                                 "gl=us", "en", "7d", "v1", "policy-v1")
        self.assertEqual(cache_b.get(other_scope, grant=GRANT, now=NOW)["reason"], "CACHE_MISS")

    def test_monotonic_cache_hit_uses_read_only_batch_and_clock_regression_falls_back(self):
        class CountingStore:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.reads = self.transactions = 0

            def read_many(self, keys):
                self.reads += 1
                return self.wrapped.read_many(keys)

            def transact(self, key, operation):
                self.transactions += 1
                return self.wrapped.transact(key, operation)

        store = CountingStore(FirestoreAtomicStateStore(self.client))
        cache = SharedCache(store, monotonic_read_clock=True)
        cache.put(SCOPE, grant=GRANT, origin="LIVE", payload={"value": "cached"},
                  fetched_at=NOW, now=NOW)
        store.reads = store.transactions = 0

        hit = cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=5))
        self.assertEqual(hit["status"], "CACHED")
        self.assertEqual((store.reads, store.transactions), (1, 0))

        regressed = cache.get(SCOPE, grant=GRANT, now=NOW - timedelta(seconds=1))
        self.assertEqual((regressed["status"], regressed["reason"]),
                         ("REJECTED", "INVALID_CACHE_TIME"))
        self.assertEqual((store.reads, store.transactions), (2, 1))

    def test_all_four_source_scopes_round_trip_without_cross_source_data(self):
        writer = create_firestore_shared_cache(self.client, max_inline_bytes=64)
        reader = create_firestore_shared_cache(self.client, max_inline_bytes=64)
        source_ids = (WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID)
        scopes = {}
        for source_id in source_ids:
            capability = source_capability(source_id)
            grant = CacheGrant(source_id, "four-source-policy", NOW + timedelta(days=1),
                               max_fresh_seconds=600, max_stale_seconds=1200,
                               max_retention_seconds=1800, allowed=True)
            scope = CacheScope(
                source_id=source_id, provider_id=capability.provider_id,
                query=f"query:{source_id}", plan_fingerprint="a" * 64,
                market="US", language="en", window="24h",
                parser_version=f"source-v{capability.capability_version}",
                policy_version=grant.policy_version,
            )
            scopes[source_id] = scope
            writer.put(scope, grant=grant, origin="LIVE", payload={
                "source_id": source_id, "status": "SUCCESS", "data_mode": "LIVE",
                "records": [{"title": source_id, "detail": "x" * 256}], "record_count": 1,
            }, fetched_at=NOW, now=NOW)

        self.assertEqual(len({scope.key for scope in scopes.values()}), len(source_ids))
        for source_id, scope in scopes.items():
            result = reader.get(
                scope, grant=CacheGrant(source_id, "four-source-policy", NOW + timedelta(days=1),
                                        600, 1200, 1800, True),
                now=NOW + timedelta(seconds=5),
            )
            self.assertEqual(result["status"], "CACHED")
            self.assertEqual(result["payload"]["source_id"], source_id)

    def test_google_candidates_are_read_from_the_shared_source_cache(self):
        cache = SharedCache(self.atomic, payload_blobs=self.blobs)
        grant = CacheGrant(
            GOOGLE_SOURCE_ID, "four-source-policy", NOW + timedelta(days=7),
            max_fresh_seconds=3600, max_stale_seconds=7 * 86400,
            max_retention_seconds=7 * 86400, allowed=True,
        )
        google_handler = GoogleTrendsDiscoveryHandler(
            policy_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            utcnow=lambda: NOW,
            cache=cache,
            resolve_cache_grant=lambda source_id, _now: (
                grant if source_id == GOOGLE_SOURCE_ID else None
            ),
            data_mode="LIVE",
        )
        scope, writer_grant = google_handler._cache_scope(None)
        self.assertEqual(scope.query, "google-trends-trending-now:US:en:24h")
        self.assertEqual(scope.parser_version, TRENDING_NOW_PARSER_VERSION)
        self.assertIs(writer_grant, grant)
        keyword = "embroidered linen shirt"
        identity = hashlib.sha256(keyword.encode()).hexdigest()
        record = {
            "source_id": GOOGLE_SOURCE_ID,
            "data_mode": "LIVE",
            "plan_id": "google-trending-now-us-en",
            "plan_version": 1,
            "canonical_keyword": keyword,
            "canonical_keyword_identity": identity,
            "fetched_at": NOW.isoformat(),
            "provider_data_at": NOW.isoformat(),
            "trend_clusters": [{"active": True, "increase_percentage": 35,
                                "categories": [{"name": "Home & Living"}]}],
        }
        cache.put(
            scope, grant=grant, origin="LIVE",
            payload={"status": "SUCCESS", "data_mode": "LIVE", "records": [record]},
            fetched_at=NOW, now=NOW,
        )

        rows = _shared_google_candidate_rows(
            cache, lambda source_id, _now: grant if source_id == GOOGLE_SOURCE_ID else None,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record_id"], identity)
        self.assertEqual(rows[0]["canonical_keyword"], keyword)
        self.assertEqual(rows[0]["source_id"], GOOGLE_SOURCE_ID)
        self.assertEqual(rows[0]["data_mode"], "LIVE")
        self.assertEqual(rows[0]["fetched_at"], NOW.isoformat())
        self.assertEqual(rows[0]["evidence"]["fetched_at"], NOW.isoformat())

    def test_external_cache_missing_blob_is_unavailable(self):
        cache = SharedCache(self.atomic, payload_blobs=self.blobs, max_inline_bytes=32)
        cache.put(SCOPE, grant=GRANT, origin="SYNTHETIC", payload={"value": "x" * 500},
                  fetched_at=NOW, now=NOW)
        state_ref = f"_ts_d5_coordination/{SCOPE.key}"
        ref = self.client.data[state_ref]["payload_ref"]
        self.client.data.pop(f"_ts_d5_cache_blobs/{ref['digest']}")
        result = cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=1))
        self.assertEqual((result["status"], result["reason"]),
                         ("UNAVAILABLE", "CACHE_BLOB_UNAVAILABLE"))

    def test_external_cache_same_observation_is_idempotent_and_conflicts_fail_closed(self):
        cache = SharedCache(self.atomic, payload_blobs=self.blobs, max_inline_bytes=32)
        payload = {"value": "x" * 500}
        cache.put(SCOPE, grant=GRANT, origin="SYNTHETIC", payload=payload,
                  fetched_at=NOW, now=NOW)
        cache.put(SCOPE, grant=GRANT, origin="SYNTHETIC", payload=payload,
                  fetched_at=NOW, now=NOW + timedelta(seconds=1))
        result = cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=2))
        self.assertEqual(result["payload"], payload)
        self.assertEqual(result["fetched_at"], NOW.isoformat())
        with self.assertRaisesRegex(ValueError, "conflicting cache entry"):
            cache.put(SCOPE, grant=GRANT, origin="SYNTHETIC", payload={"value": "y" * 500},
                      fetched_at=NOW, now=NOW + timedelta(seconds=3))
        self.assertEqual(cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=4))["payload"], payload)
        inline_writer = SharedCache(self.atomic, payload_blobs=self.blobs, max_inline_bytes=1024)
        with self.assertRaisesRegex(ValueError, "conflicting cache entry"):
            inline_writer.put(SCOPE, grant=GRANT, origin="SYNTHETIC", payload=payload,
                              fetched_at=NOW, now=NOW + timedelta(seconds=5))
        self.assertEqual(cache.get(SCOPE, grant=GRANT, now=NOW + timedelta(seconds=6))["payload"], payload)

    def test_four_source_handler_returns_youtube_cache_before_provider_call(self):
        source = YOUTUBE_SOURCE_ID
        capability = source_capability(source)
        cache_grant = CacheGrant(source, "youtube-cache-policy", NOW + timedelta(days=1),
                                 max_fresh_seconds=600, max_stale_seconds=1200,
                                 max_retention_seconds=1800, allowed=True)
        scope = CacheScope(
            source_id=source, provider_id=capability.provider_id,
            query=DEFAULT_QUERY_PLAN.fingerprint,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            market=DEFAULT_QUERY_PLAN.market,
            language=DEFAULT_QUERY_PLAN.language or "all",
            window=DEFAULT_QUERY_PLAN.time_filter_rule,
            parser_version=f"source-v{capability.capability_version}",
            policy_version=cache_grant.policy_version,
        )
        payload = {"status": "SUCCESS", "data_mode": "LIVE", "records": [],
                   "record_count": 0, "coverage": {"provider_request_count": 1}}
        writer = create_firestore_shared_cache(self.client, max_inline_bytes=32)
        writer.put(scope, grant=cache_grant, origin="LIVE", payload=payload,
                   fetched_at=NOW, now=NOW)

        cache = create_firestore_shared_cache(self.client, max_inline_bytes=32)
        provider_calls = []

        async def provider():
            provider_calls.append("called")
            return {"status": "SUCCESS", "data_mode": "LIVE", "records": [], "record_count": 0}

        handlers = D5SourceHandlers(
            query_collectors={GOOGLE_SOURCE_ID: None, BING_SOURCE_ID: None, source: None},
            wikimedia=None, plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=1, max_requests=1),
            shared_cache=cache, resolve_cache_grant=lambda _source, _now: cache_grant,
            unavailable_mode="LIVE", utcnow=lambda: NOW + timedelta(seconds=5),
        )
        result = asyncio.run(handlers._with_cache(source, None, provider))
        self.assertEqual(result["delivery_state"], "CACHED")
        self.assertEqual(result["provider_request_count"], 0)
        self.assertEqual(provider_calls, [])

    def test_successful_empty_results_are_cached_without_a_second_provider_call(self):
        source_ids = (WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID)
        grants = {
            source_id: CacheGrant(
                source_id, "empty-result-cache-policy", NOW + timedelta(days=1),
                max_fresh_seconds=600, max_stale_seconds=1200,
                max_retention_seconds=1800, allowed=True,
            )
            for source_id in source_ids
        }
        cache = create_firestore_shared_cache(self.client)
        handlers = D5SourceHandlers(
            query_collectors={GOOGLE_SOURCE_ID: None, BING_SOURCE_ID: None,
                              YOUTUBE_SOURCE_ID: None},
            wikimedia=None,
            plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=1, max_requests=1),
            shared_cache=cache,
            resolve_cache_grant=lambda source_id, _now: grants.get(source_id),
            unavailable_mode="LIVE",
            utcnow=lambda: NOW + timedelta(seconds=5),
        )

        for source_id in source_ids:
            with self.subTest(source_id=source_id):
                provider_calls = []

                async def provider():
                    provider_calls.append(source_id)
                    return {
                        "status": "SUCCESS", "data_mode": "LIVE",
                        "delivery_state": "FRESH", "records": [], "record_count": 0,
                        "coverage": {"provider_request_count": 1},
                        "provider_request_count": 1,
                    }

                first = asyncio.run(handlers._with_cache(source_id, None, provider))
                second = asyncio.run(handlers._with_cache(source_id, None, provider))

                self.assertEqual(first["delivery_state"], "FRESH")
                self.assertEqual(first["provider_request_count"], 1)
                self.assertEqual(second["status"], "SUCCESS")
                self.assertEqual(second["delivery_state"], "CACHED")
                self.assertEqual(second["provider_request_count"], 0)
                self.assertEqual(second["coverage"]["cached_provider_request_count"], 1)
                self.assertEqual(provider_calls, [source_id])

    def test_public_admission_quota_ledger_is_shared_between_adapter_instances(self):
        policy = AdmissionPolicy(
            enabled_sources=frozenset({GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID}),
            pools={"serpapi": PoolLimit(2, 4, 2, 4, 1),
                   "youtube": PoolLimit(1, 3, 1, 3, 1)},
            plan_fingerprint="a" * 64, cooldown_seconds=0, lease_seconds=10, enabled=True,
        )
        first = PublicAdmission(self.atomic, policy, state_key="public")
        second = PublicAdmission(FirestoreAtomicStateStore(self.client), policy,
                                 state_key="public")
        job_id = first.admit("shared-firestore-quota-job", now=NOW)["job_id"]
        fence = first.claim(job_id, "worker", now=NOW)["fence"]
        self.assertEqual(first.reserve_attempt(
            job_id, "worker", fence, source_id=GOOGLE_SOURCE_ID,
            attempt_id="google-physical-attempt-001", now=NOW,
        ), "NEW")
        self.assertEqual(second.reserve_attempt(
            job_id, "worker", fence, source_id=BING_SOURCE_ID,
            attempt_id="bing-physical-attempt-001", now=NOW,
        ), "NEW")
        with self.assertRaisesRegex(AdmissionDenied, "QUOTA_EXHAUSTED"):
            second.reserve_attempt(
                job_id, "worker", fence, source_id=GOOGLE_SOURCE_ID,
                attempt_id="google-physical-attempt-002", now=NOW,
            )


class FirestoreSharedFetchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_read_composition_loads_saved_snapshot_without_worker(self):
        client = SharedRuntimeFakeClient()
        with patch.dict(os.environ, {"VERCEL": "1"}):
            async with vercel_collection_read_composition(
                client,
                user_agent="TrendScoutTest/0.1",
                utcnow=lambda: NOW,
            ) as facade:
                self.assertIsInstance(facade.collection_service, CollectionService)
                self.assertEqual(facade.collection_snapshot.version, 0)
                self.assertEqual(facade.available_sources, frozenset({
                    WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID,
                    BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
                }))
                self.assertFalse(hasattr(facade, "admission"))
                self.assertFalse(hasattr(facade, "worker"))

        self.assertEqual(client.batch_gets, 1)
        self.assertEqual(client.document_gets, 0)

    async def test_vercel_capabilities_expose_all_four_shared_live_sources(self):
        client = SharedRuntimeFakeClient()
        environment = {
            "VERCEL": "1",
            "TREND_SCOUT_ENVIRONMENT": "vercel",
            "TREND_SCOUT_RUN_MODE": "live",
            "TREND_SCOUT_READ_ONLY": "true",
            "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
            "TREND_SCOUT_VERCEL_SHARED_LIVE_ENABLED": "true",
            "GOOGLE_CLOUD_PROJECT": PRODUCTION_FIRESTORE_PROJECT_ID,
            "GOOGLE_WIF_AUDIENCE": (
                "//iam.googleapis.com/projects/556422620769/locations/global/"
                "workloadIdentityPools/trendscout-vercel/providers/vercel-production"
            ),
            "TREND_SCOUT_GOOGLE_TRENDS_APPROVED": "true",
            "TREND_SCOUT_GOOGLE_SERP_APPROVED": "true",
            "TREND_SCOUT_BING_SERP_APPROVED": "true",
            "TREND_SCOUT_SERPAPI_ACCOUNT_API_APPROVED": "true",
            "TREND_SCOUT_YOUTUBE_LIVE_APPROVED": "true",
            "SERPAPI_API_KEY": "test-serpapi-key",
            "YOUTUBE_API_KEY": "test-youtube-api-key",
        }
        transaction_patchers = [
            patch("app.storage.firestore_atomic_state.firestore.transactional", fake_transactional),
            patch("app.storage.firestore_snapshot_blobs.firestore.transactional", fake_transactional),
        ]
        send = patch.object(httpx.AsyncHTTPTransport, "handle_async_request")
        with patch.dict(os.environ, environment, clear=True):
            for patcher in transaction_patchers:
                patcher.start()
            send_mock = send.start()
            self.addCleanup(lambda: [patcher.stop() for patcher in reversed(
                [*transaction_patchers, send]
            )])
            app = create_app(
                settings=Settings.from_environment(environment),
                vercel_firestore_client_factory=lambda _token: client,
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://trend-scout.example",
                ) as browser:
                    response = await browser.get(
                        "/api/v1/capabilities",
                        headers={"x-vercel-oidc-token": "synthetic-test-token"},
                    )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["vercel_shared_live"])
        self.assertFalse(body["read_only"])
        self.assertTrue(body["source_selection_supported"])
        enabled = {
            source["source_id"] for source in body["sources"]
            if source["enabled_for_current_operation"]
        }
        self.assertEqual(enabled, {
            WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
        })
        sources = {source["source_id"]: source for source in body["sources"]}
        self.assertTrue(any(
            "Vercel Shared LIVE" in limitation
            for limitation in sources[GOOGLE_SOURCE_ID]["known_limitations"]
        ))
        self.assertFalse(any(
            "Offline mock transport only" in limitation
            for source_id in (GOOGLE_SOURCE_ID, BING_SOURCE_ID)
            for limitation in sources[source_id]["known_limitations"]
        ))
        self.assertTrue(any(
            "Provider quota status is unknown" in limitation
            for limitation in sources[YOUTUBE_SOURCE_ID]["known_limitations"]
        ))
        self.assertEqual(client.batch_gets, 2)
        send_mock.assert_not_called()

    async def test_vercel_shared_live_composes_all_sources_without_provider_calls(self):
        client = SharedRuntimeFakeClient()
        environment = {
            "VERCEL": "1",
            "VERCEL_ENV": "production",
            "TREND_SCOUT_GOOGLE_TRENDS_APPROVED": "true",
            "TREND_SCOUT_GOOGLE_SERP_APPROVED": "true",
            "TREND_SCOUT_BING_SERP_APPROVED": "true",
            "TREND_SCOUT_SERPAPI_ACCOUNT_API_APPROVED": "true",
            "TREND_SCOUT_YOUTUBE_LIVE_APPROVED": "true",
            "SERPAPI_API_KEY": "test-serpapi-key",
            "YOUTUBE_API_KEY": "test-youtube-api-key",
        }
        transaction_patchers = [
            patch("app.storage.firestore_atomic_state.firestore.transactional", fake_transactional),
            patch("app.storage.firestore_snapshot_blobs.firestore.transactional", fake_transactional),
        ]
        send = patch.object(httpx.AsyncHTTPTransport, "handle_async_request")
        with patch.dict(os.environ, environment, clear=True):
            for patcher in transaction_patchers:
                patcher.start()
            send_mock = send.start()
            self.addCleanup(lambda: [patcher.stop() for patcher in reversed(
                [*transaction_patchers, send]
            )])

            async with vercel_shared_live_composition(
                client,
                user_agent="TrendScoutTestBot/0.1 (https://example.invalid/contact)",
                utcnow=lambda: NOW,
            ) as facade:
                from app.collections.firestore_snapshot import FirestoreCollectionSnapshot
                from app.collections.service import CollectionService

                self.assertEqual(facade.data_mode, "LIVE")
                self.assertTrue(facade.firestore_shared_live)
                self.assertTrue(facade.vercel_shared_live)
                self.assertIsInstance(facade.collection_service, CollectionService)
                self.assertIsInstance(facade.collection_snapshot, FirestoreCollectionSnapshot)
                self.assertEqual(facade.available_sources, frozenset({
                    WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID,
                    YOUTUBE_SOURCE_ID,
                }))
                self.assertEqual(facade.worker.source_order, (
                    GOOGLE_SOURCE_ID, BING_SOURCE_ID,
                    WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID,
                ))
                self.assertEqual(facade.google_capabilities["runtime_scope"],
                                 "VERCEL_SHARED_LIVE")
                self.assertEqual(facade.google_capabilities["bing_serp_verification"],
                                 "LIVE_CONFIGURED")
                self.assertEqual(facade.youtube_capabilities["live_status"],
                                 "LIVE_CONFIGURED")
                self.assertEqual(facade.youtube_capabilities["live_host_status"],
                                 "VERCEL_SHARED_RUNTIME")
                self.assertEqual(facade.google_account_quota_state["status"], "NOT_CHECKED")
                self.assertTrue(facade.owner_fresh_acceptance_once)
                self.assertIsNone(facade.provider_retry_after_seconds)
                self.assertIsNotNone(facade.journal.snapshot_blobs._read_cache)

        send_mock.assert_not_called()

    async def test_normal_google_composition_selects_firestore_only_when_injected(self):
        client = SharedRuntimeFakeClient()

        class NoCredentials:
            def resolve(self, _reference):
                return None

        with tempfile.TemporaryDirectory() as directory:
            from app.collections.store import CollectionStore

            collection_store = CollectionStore(Path(directory) / "collections.sqlite")
            send = patch.object(httpx.AsyncHTTPTransport, "handle_async_request")
            transaction_patchers = [
                patch("app.storage.firestore_atomic_state.firestore.transactional", fake_transactional),
                patch("app.storage.firestore_snapshot_blobs.firestore.transactional", fake_transactional),
                send,
            ]
            started = [patcher.start() for patcher in transaction_patchers]
            send_mock = started[-1]
            self.addCleanup(lambda: [patcher.stop() for patcher in reversed(transaction_patchers)])

            async with normal_google_live_composition(
                user_agent="TrendScoutTestBot/0.1 (https://example.invalid/contact)",
                google_settings=GoogleLiveSettings(),
                credential_provider=NoCredentials(),
                collection_store=collection_store,
                preserve_expired_data=True,
                shared_firestore_client=client,
            ) as facade:
                self.assertIsInstance(facade.admission.store, FirestoreAtomicStateStore)
                self.assertIsInstance(facade.journal.snapshot_blobs, FirestoreSnapshotBlobs)
                self.assertIs(facade.admission.store._client, client)
                self.assertIs(facade.journal.snapshot_blobs._client, client)
                self.assertTrue(facade.firestore_shared_live)
                self.assertFalse(facade.normal_google_live)
                self.assertTrue(callable(facade.provider_retry_after_seconds))
                self.assertGreaterEqual(client.batch_gets, 1)
                self.assertEqual(
                    facade.normal_google_live_sources,
                    frozenset({WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID}),
                )
                self.assertIsNone(facade.youtube_retention_cleanup)
                send_mock.assert_not_called()

    async def test_local_fetch_api_uses_shared_firestore_cache_and_charges_quota_once(self):
        client = SharedRuntimeFakeClient()
        state_key = "firestore-fetch-integration"
        policy = AdmissionPolicy(
            enabled_sources=frozenset({GOOGLE_SOURCE_ID}),
            pools={"serpapi": PoolLimit(1, 1, 1, 1, 1,
                                        max_inflight=1, attempt_timeout_seconds=10)},
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            max_starts_per_window=10,
            cooldown_seconds=0,
            lease_seconds=30,
            max_attempts=1,
            max_job_seconds=60,
            receipt_seconds=120,
            enabled=True,
        )
        grant = CacheGrant(
            GOOGLE_SOURCE_ID, "policy-v1", NOW + timedelta(days=1),
            max_fresh_seconds=600, max_stale_seconds=1200,
            max_retention_seconds=1800, allowed=True,
        )
        scope = CacheScope(
            source_id=GOOGLE_SOURCE_ID,
            provider_id="serpapi_google",
            query="shared-fetch-integration",
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            market="US",
            language="en",
            window="7d",
            parser_version="source-v1",
            policy_version=grant.policy_version,
        )
        provider_calls = []
        compositions = []

        def handlers_factory(cache):
            quota = PublicAdmission(cache.store, policy, state_key=state_key)
            runner = GuardedAttemptRunner(quota, clock=lambda: NOW)

            async def handler(context):
                cached = cache.get(scope, grant=grant, now=NOW)
                if cached.get("status") == "CACHED":
                    payload = dict(cached["payload"])
                    payload["delivery_state"] = "CACHED"
                    payload["provider_request_count"] = 0
                    payload["coverage"] = {
                        **payload.get("coverage", {}),
                        "provider_request_count": 0,
                        "cache_fetched_at": cached.get("fetched_at"),
                    }
                    return payload

                async def provider_request():
                    provider_calls.append(context.job_id)
                    return {
                        "status": "SUCCESS",
                        "data_mode": "LIVE",
                        "delivery_state": "FRESH",
                        "records": [],
                        "record_count": 0,
                        "provider_request_count": 1,
                        "coverage": {"provider_request_count": 1},
                    }

                outcome = await runner.run(
                    job_id=context.job_id,
                    owner=context.owner,
                    fence=context.fence,
                    source_id=GOOGLE_SOURCE_ID,
                    attempt_id=f"fixture-{context.job_id}",
                    timeout_seconds=5,
                    request=provider_request,
                )
                if outcome.status != "COMPLETED":
                    return {
                        "status": "FAILED", "data_mode": "LIVE",
                        "record_count": 0, "records": [],
                        "error_code": "FIXTURE_ATTEMPT_FAILED",
                    }
                result = outcome.value
                cache.put(
                    scope, grant=grant, origin="LIVE", payload=result,
                    fetched_at=NOW, now=NOW,
                )
                return result

            handler.__trendscout_data_mode__ = "LIVE"
            return {GOOGLE_SOURCE_ID: handler}

        @asynccontextmanager
        async def composition():
            value = create_firestore_shared_live_composition(
                client,
                policy=policy,
                state_key=state_key,
                handlers_factory=handlers_factory,
                resolve_grant=lambda source, now: CheckpointGrant(
                    source, "LIVE", "fixture-retention", now + timedelta(minutes=2),
                    120, True,
                ),
                resolve_processing_grants=lambda _now: (),
                utcnow=lambda: NOW,
            )
            value.public_fetch.normal_google_live_sources = frozenset({GOOGLE_SOURCE_ID})
            compositions.append(value)
            yield value.public_fetch

        settings = Settings.from_environment({
            "TREND_SCOUT_ENVIRONMENT": "local",
            "TREND_SCOUT_RUN_MODE": "live",
            "TREND_SCOUT_READ_ONLY": "false",
            "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
            "WIKIMEDIA_USER_AGENT": "TrendScout offline Firestore integration test",
        })
        transaction_patchers = [
            patch("app.storage.firestore_atomic_state.firestore.transactional", fake_transactional),
            patch("app.storage.firestore_snapshot_blobs.firestore.transactional", fake_transactional),
        ]
        for patcher in transaction_patchers:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(transaction_patchers)])

        with tempfile.TemporaryDirectory() as directory:
            from app.collections.store import CollectionStore

            collection_store = CollectionStore(Path(directory) / "collections.sqlite")
            store_patcher = patch(
                "app.collections.store.CollectionStore.local", return_value=collection_store
            )
            store_patcher.start()
            self.addCleanup(store_patcher.stop)

            for key in (
                "firestore-shared-runtime-first-request",
                "firestore-shared-runtime-second-request",
            ):
                app = create_normal_google_live_app(
                    enabled=True,
                    workers=1,
                    composition_factory=composition,
                    settings=settings,
                )
                inner = app.app
                async with inner.router.lifespan_context(inner):
                    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://127.0.0.1"
                    ) as session:
                        accepted = await session.post(
                            "/api/v1/fetches", json={}, headers={"Idempotency-Key": key}
                        )
                        self.assertEqual(accepted.status_code, 202, accepted.text)
                        status = await session.get(accepted.json()["status_url"])
                        self.assertEqual(status.status_code, 200, status.text)
                        source_result = status.json()["sources"][0]
                        self.assertEqual(source_result["status"], "SUCCESS")
                        self.assertEqual(
                            source_result["delivery_state"],
                            "FRESH" if len(compositions) == 1 else "CACHED",
                        )
                        self.assertEqual(
                            source_result["provider_request_count"],
                            1 if len(compositions) == 1 else 0,
                        )

        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(
            compositions[-1].admission.quota_snapshot("serpapi", now=NOW)["daily"]["used"],
            1,
        )
        self.assertTrue(compositions[-1].public_fetch.firestore_shared_live)

    async def test_vercel_wikimedia_composition_fetches_once_then_reuses_firestore_cache(self):
        client = SharedRuntimeFakeClient()
        now = [NOW]
        provider_requests = []

        def response(request):
            provider_requests.append(request.url.path)
            date_parts = request.url.path.rstrip("/").split("/")[-3:]
            observation_day = datetime.strptime("/".join(date_parts), "%Y/%m/%d").date()
            return httpx.Response(200, json={"items": [{
                "country": "US",
                "access": "all-access",
                "year": observation_day.strftime("%Y"),
                "month": observation_day.strftime("%m"),
                "day": observation_day.strftime("%d"),
                "articles": [{
                    "article": "Coffee",
                    "project": "en.wikipedia",
                    "views_ceil": 100,
                    "rank": 1,
                }],
            }]}, request=request)

        async def controlled_network(_gateway_cls, approvals, runner, *, user_agent, **kwargs):
            return _LiveMockWikimediaGateway.offline_fixture(
                approvals,
                runner,
                user_agent=user_agent,
                transport=httpx.MockTransport(response),
                **kwargs,
            )

        transaction_patchers = [
            patch("app.storage.firestore_atomic_state.firestore.transactional", fake_transactional),
            patch("app.storage.firestore_snapshot_blobs.firestore.transactional", fake_transactional),
        ]
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=True), patch.object(
            WikimediaTopPageGateway,
            "controlled_network",
            new=classmethod(controlled_network),
        ):
            for patcher in transaction_patchers:
                patcher.start()
            self.addCleanup(lambda: [patcher.stop() for patcher in reversed(transaction_patchers)])

            for index, idempotency_key in enumerate((
                "vercel-wikimedia-shared-cache-first",
                "vercel-wikimedia-shared-cache-second",
            )):
                async with vercel_wikimedia_live_composition(
                    client,
                    user_agent="TrendScoutTest/0.1 (https://example.invalid/contact)",
                    utcnow=lambda: now[0],
                ) as facade:
                    self.assertIsInstance(facade.admission.store, FirestoreAtomicStateStore)
                    self.assertIs(facade.admission.store._client, client)
                    self.assertIs(facade.journal.snapshot_blobs._client, client)
                    self.assertEqual(facade.available_sources, frozenset({WIKIMEDIA_SOURCE_ID}))
                    accepted = facade.submit(
                        idempotency_key, source_ids=[WIKIMEDIA_SOURCE_ID]
                    )
                    result = await facade.run_once(f"vercel-worker-{index}")
                    status = facade.status(accepted["job_id"])
                    source = status["sources"][0]
                    self.assertEqual(result["state"], "COMPLETED")
                    self.assertEqual(source["status"], "SUCCESS")
                    self.assertEqual(
                        source["delivery_state"], "FRESH" if index == 0 else "CACHED"
                    )
                    self.assertEqual(
                        source["provider_request_count"], 1 if index == 0 else 0
                    )
                now[0] += timedelta(seconds=2)

        self.assertEqual(len(provider_requests), 1)

    async def test_vercel_and_windows2_compositions_share_wikimedia_cache_and_quota(self):
        client = SharedRuntimeFakeClient()
        now = [NOW]
        provider_requests = []

        def response(request):
            provider_requests.append(request.url.path)
            date_parts = request.url.path.rstrip("/").split("/")[-3:]
            observation_day = datetime.strptime("/".join(date_parts), "%Y/%m/%d").date()
            return httpx.Response(200, json={"items": [{
                "country": "US", "access": "all-access",
                "year": observation_day.strftime("%Y"),
                "month": observation_day.strftime("%m"),
                "day": observation_day.strftime("%d"),
                "articles": [{
                    "article": "Coffee", "project": "en.wikipedia",
                    "views_ceil": 100, "rank": 1,
                }],
            }]}, request=request)

        async def controlled_network(_gateway_cls, approvals, runner, *, user_agent, **kwargs):
            return _LiveMockWikimediaGateway.offline_fixture(
                approvals, runner, user_agent=user_agent,
                transport=httpx.MockTransport(response), **kwargs,
            )

        class NoCredentials:
            def resolve(self, _reference):
                return None

        transaction_patchers = [
            patch("app.storage.firestore_atomic_state.firestore.transactional", fake_transactional),
            patch("app.storage.firestore_snapshot_blobs.firestore.transactional", fake_transactional),
        ]
        with patch.object(
            WikimediaTopPageGateway,
            "controlled_network",
            new=classmethod(controlled_network),
        ):
            for patcher in transaction_patchers:
                patcher.start()
            self.addCleanup(lambda: [patcher.stop() for patcher in reversed(transaction_patchers)])

            with patch.dict(os.environ, {"VERCEL": "1"}, clear=True):
                async with vercel_wikimedia_live_composition(
                    client,
                    user_agent="TrendScoutTest/0.1 (https://example.invalid/contact)",
                    utcnow=lambda: now[0],
                ) as vercel:
                    accepted = vercel.submit(
                        "vercel-windows2-shared-cache-first",
                        source_ids=[WIKIMEDIA_SOURCE_ID],
                    )
                    await vercel.run_once("vercel-runtime")
                    vercel_status = vercel.status(accepted["job_id"])
                    self.assertEqual(vercel_status["sources"][0]["delivery_state"], "FRESH")
                    vercel_history = vercel.history(limit=20)
                    self.assertEqual([item["fetch_id"] for item in vercel_history],
                                     [accepted["job_id"]])
                vercel_quota = vercel.admission.quota_snapshot("wikimedia", now=now[0])

            now[0] += timedelta(seconds=2)
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {}, clear=True
            ):
                from app.collections.store import CollectionStore

                collection_store = CollectionStore(Path(directory) / "collections.sqlite")
                async with normal_google_live_composition(
                    user_agent="TrendScoutTest/0.1 (https://example.invalid/contact)",
                    google_settings=replace(
                        GoogleLiveSettings(),
                        trends_approved=True,
                        serp_approved=True,
                        bing_approved=True,
                        policy_migration_approved=True,
                        account_api_approved=True,
                    ),
                    credential_provider=NoCredentials(),
                    collection_store=collection_store,
                    preserve_expired_data=True,
                    shared_firestore_client=client,
                    utcnow=lambda: now[0],
                ) as windows2:
                    self.assertEqual(
                        windows2.admission.policy_digest,
                        vercel.admission.policy_digest,
                    )
                    accepted = windows2.submit(
                        "windows2-vercel-shared-cache-second",
                        source_ids=[WIKIMEDIA_SOURCE_ID],
                    )
                    await windows2.run_once("windows2-runtime")
                    windows2_status = windows2.status(accepted["job_id"])
                    self.assertEqual(windows2_status["sources"][0]["status"], "SUCCESS")
                    self.assertEqual(
                        windows2_status["sources"][0]["delivery_state"], "CACHED"
                    )
                    self.assertEqual(
                        windows2_status["sources"][0]["provider_request_count"], 0
                    )
                    shared_history = windows2.history(limit=20)
                    self.assertEqual(
                        {item["fetch_id"] for item in shared_history},
                        {vercel_history[0]["fetch_id"], accepted["job_id"]},
                    )
                    windows2_quota = windows2.admission.quota_snapshot(
                        "wikimedia", now=now[0]
                    )

        self.assertEqual(len(provider_requests), 1)
        self.assertEqual(vercel_quota["daily"]["used"], 1)
        self.assertEqual(windows2_quota["daily"]["used"], 1)


if __name__ == "__main__":
    unittest.main()
