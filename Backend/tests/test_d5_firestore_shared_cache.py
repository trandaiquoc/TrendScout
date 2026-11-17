"""Offline transactional contract tests for Firestore-backed shared cache blobs."""

import asyncio
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.jobs.public_admission import AdmissionDenied, AdmissionPolicy, PoolLimit, PublicAdmission
from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
)
from app.sources.guarded_collector import CollectionLimits
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
from app.storage.firestore_shared_cache import create_firestore_shared_cache
from app.storage.firestore_snapshot_blobs import FirestoreSnapshotBlobs
from app.workers.d5_source_handlers import D5SourceHandlers

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
        if self.client.fail_write_at is not None and len(self.pending) == self.client.fail_write_at:
            raise RuntimeError("injected transaction failure")
        self.pending.append(("set", reference.path, deepcopy(value)))

    def delete(self, reference):
        self.pending.append(("delete", reference.path, None))


class FakeClient:
    def __init__(self):
        self.data, self.fail_write_at = {}, None

    def collection(self, name):
        return FakeCollection(self, name)

    def transaction(self, max_attempts):
        return FakeTransaction(self)


def fake_transactional(callback):
    def run(transaction):
        result = callback(transaction)
        updated = deepcopy(transaction.client.data)
        for action, path, value in transaction.pending:
            if action == "set":
                updated[path] = value
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
        self.assertEqual(
            self.blobs.put(records, now=NOW, retain_until=NOW + timedelta(seconds=40)), ref
        )
        self.assertEqual(reopened.get(ref, now=NOW + timedelta(seconds=35)), records)
        with self.assertRaisesRegex(ValueError, "expired"):
            reopened.get(ref, now=NOW + timedelta(seconds=40))

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


if __name__ == "__main__":
    unittest.main()
