"""Offline transactional contract tests for Firestore-backed shared cache blobs."""

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app.google_live_settings import GoogleLiveSettings
from app.jobs.public_admission import AdmissionDenied, AdmissionPolicy, PoolLimit, PublicAdmission
from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant
from app.main import create_normal_google_live_app
from app.orchestration.firestore_shared_live_composition import (
    PRODUCTION_FIRESTORE_PROJECT_ID,
    create_firestore_shared_live_composition,
)
from app.orchestration.normal_google_live_composition import normal_google_live_composition
from app.settings import Settings
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


class SharedRuntimeFakeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.project = PRODUCTION_FIRESTORE_PROJECT_ID
        self._database = "(default)"


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


class FirestoreSharedFetchIntegrationTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
