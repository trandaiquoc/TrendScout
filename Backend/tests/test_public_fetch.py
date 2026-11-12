"""Public Fetch admission stays bounded and offline-only until live approval."""

import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from test_d5_live_gateway import Approvals, Credentials
from test_d5_public_admission import NOW, coordinator, policy
from test_d5_wikimedia_gateway import USER_AGENT
from test_d5_wikimedia_gateway import response as wikimedia_fixture
from test_runs import fixture

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PoolLimit
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant
from app.main import create_app
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.guarded_collector import CollectionLimits, GuardedQueryCollector
from app.sources.live_gateway import LivePageGateway
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.fake import FakeStorage
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

TEST_ENV = {
    "TREND_SCOUT_ENVIRONMENT": "test",
    "TREND_SCOUT_RUN_MODE": "synthetic",
    "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true",
    "TREND_SCOUT_RUN_COOLDOWN_SECONDS": "0",
    "TREND_SCOUT_OPERATOR_TOKEN": "test-only-legacy-token-123456789",
}
_DISTRIBUTED_SOURCES = {
    WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
}


def distributed_fixture_service(state_path):
    config = policy(
        enabled_sources=frozenset(_DISTRIBUTED_SOURCES),
        pools={"wikimedia": PoolLimit(2, 5, 2, 5, 1),
               "serpapi": PoolLimit(2, 5, 2, 5, 1),
               "youtube": PoolLimit(1, 3, 1, 3, 1)},
        plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        cooldown_seconds=0, max_status_polls=20,
    )
    admission = coordinator(state_path, config)
    handlers = {}
    for source_id in _DISTRIBUTED_SOURCES:
        def handler(context):
            return {"status": "SUCCESS", "data_mode": "SYNTHETIC",
                    "records": [{"source_id": context.source_id,
                                 "keyword": "synthetic fixture"}],
                    "record_count": 1}
        handler.__trendscout_data_mode__ = "SYNTHETIC"
        handlers[source_id] = handler

    def resolve_grant(source_id, now):
        return CheckpointGrant(source_id, "SYNTHETIC", "api-fixture-policy-v1",
                               now + timedelta(hours=1), 300, True)

    worker = DistributedFetchWorker(admission, handlers=handlers,
                                    resolve_grant=resolve_grant)
    return DistributedPublicFetch(admission, worker, resolve_grant=resolve_grant)


def distributed_cached_fixture_service(state_path):
    """Compose public API services over one local atomic file, fixtures only."""
    config = policy(
        enabled_sources=frozenset(_DISTRIBUTED_SOURCES),
        pools={"wikimedia": PoolLimit(2, 5, 2, 5, 1),
               "serpapi": PoolLimit(2, 5, 2, 5, 1),
               "youtube": PoolLimit(1, 3, 1, 3, 1)},
        plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        cooldown_seconds=0,
    )
    store = LocalSQLiteAtomicStateStore(state_path, environment_profile="test")
    admission = coordinator(state_path, config)
    runner = GuardedAttemptRunner(admission, clock=lambda: NOW)
    calls = {source_id: 0 for source_id in _DISTRIBUTED_SOURCES}
    gateways = []

    def search_handler(source_id):
        def handle(_request):
            calls[source_id] += 1
            return httpx.Response(200, json={
                "search_metadata": {"status": "Success"},
                "organic_results": [{"position": 1, "link": "https://example.org/fixture",
                                     "title": "synthetic fixture result"}],
            })
        return handle

    def youtube_handler(_request):
        calls[YOUTUBE_SOURCE_ID] += 1
        return httpx.Response(200, json={
            "kind": "youtube#searchListResponse",
            "items": [{"kind": "youtube#searchResult",
                       "id": {"kind": "youtube#video", "videoId": "AbCdEf12345"},
                       "snippet": {"title": "synthetic video",
                                   "publishedAt": "2026-11-11T00:00:00Z"}}],
        })

    collectors = {}
    for source_id in (GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID):
        handler = youtube_handler if source_id == YOUTUBE_SOURCE_ID else search_handler(source_id)
        gateway = LivePageGateway.offline_fixture(
            source_id, Credentials(), Approvals(source_id), runner,
            transport=httpx.MockTransport(handler), clock=lambda: NOW,
        )
        gateways.append(gateway)
        collectors[source_id] = GuardedQueryCollector(gateway, utcnow=lambda: NOW)

    def wikimedia_handler(_request):
        calls[WIKIMEDIA_SOURCE_ID] += 1
        return httpx.Response(200, json=wikimedia_fixture(NOW.date()))

    wikimedia = WikimediaTopPageGateway.offline_fixture(
        Approvals(WIKIMEDIA_SOURCE_ID), runner, user_agent=USER_AGENT,
        transport=httpx.MockTransport(wikimedia_handler), clock=lambda: NOW,
    )
    gateways.append(wikimedia)

    def cache_grant(source_id, now):
        return CacheGrant(source_id, "offline-fixture-cache-v1", now + timedelta(hours=1),
                          max_fresh_seconds=600, max_stale_seconds=1200,
                          max_retention_seconds=1800, allowed=True)

    handlers = D5SourceHandlers(
        query_collectors=collectors, wikimedia=wikimedia, plan=DEFAULT_QUERY_PLAN,
        limits=CollectionLimits(max_queries=1, max_pages_per_query=1, max_requests=1),
        observation_date=lambda: NOW.date(), shared_cache=SharedCache(store),
        resolve_cache_grant=cache_grant, utcnow=lambda: NOW,
    ).build()

    def checkpoint_grant(source_id, now):
        return CheckpointGrant(source_id, "SYNTHETIC", "offline-fixture-v1",
                               now + timedelta(hours=1), 300, True)

    worker = DistributedFetchWorker(
        admission, handlers=handlers, resolve_grant=checkpoint_grant, utcnow=lambda: NOW
    )
    service = DistributedPublicFetch(admission, worker, resolve_grant=checkpoint_grant,
                                    utcnow=lambda: NOW)
    return service, gateways, calls


class OfflineFixtureAdapter:
    __trendscout_offline_only__ = True
    calls = 0

    async def collect(self):
        type(self).calls += 1
        await asyncio.sleep(0.01)
        return fixture()


class PublicFetchApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = FakeStorage()
        OfflineFixtureAdapter.calls = 0
        self.app = create_app(
            settings=Settings.from_environment(TEST_ENV),
            storage=self.storage,
            adapter_factory=OfflineFixtureAdapter,
            lock_path=str(Path(self.tmp.name) / "run.lock"),
        )
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.tmp.cleanup()

    def headers(self, key="public-fetch-action-0001"):
        return {"Origin": "http://127.0.0.1", "Idempotency-Key": key}

    async def post_fetch(self, key="public-fetch-action-0001", body="{}", headers=None):
        return await self.client.post(
            "/api/v1/fetches",
            content=body,
            headers={"Content-Type": "application/json", **(headers or self.headers(key))},
        )

    async def test_anonymous_capabilities_are_safe_and_describe_all_sources(self):
        response = await self.client.get("/api/v1/capabilities")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        value = response.json()
        self.assertFalse(value["authentication_required"])
        self.assertTrue(value["public_fetch_enabled"])
        self.assertEqual(value["data_mode"], "SYNTHETIC")
        self.assertEqual(value["query_plan"]["id"], "demo-us-en")
        self.assertEqual(
            {source["source_id"] for source in value["sources"]},
            {WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID},
        )
        sources = {source["source_id"]: source for source in value["sources"]}
        self.assertEqual(sources[WIKIMEDIA_SOURCE_ID]["adapter_status"], "IMPLEMENTED")
        self.assertEqual(sources[WIKIMEDIA_SOURCE_ID]["live_verification_status"], "UNVERIFIED")
        youtube = sources[YOUTUBE_SOURCE_ID]
        self.assertEqual(youtube["adapter_status"], "IMPLEMENTED_OFFLINE_VERIFIED")
        self.assertEqual(youtube["supported_data_modes"], ["SYNTHETIC"])
        self.assertEqual(youtube["live_verification_status"], "NOT_VERIFIED")
        self.assertFalse(youtube["enabled_for_current_operation"])
        for source_id in (YOUTUBE_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID):
            self.assertEqual(sources[source_id]["approval_status"], "PENDING_APPROVAL")
            self.assertFalse(sources[source_id]["enabled_for_current_operation"])
        self.assertEqual(value["query_plan"]["request_budget_per_source"], 1)
        self.assertNotIn("Authorization", response.text)
        self.assertNotIn("password", response.text.lower())
        self.assertNotIn("api_key", response.text.lower())

    async def test_same_key_concurrent_posts_return_one_execution(self):
        first, second = await asyncio.gather(self.post_fetch(), self.post_fetch())

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["fetch_id"], second.json()["fetch_id"])
        self.assertEqual(first.json()["data_mode"], "SYNTHETIC")
        self.assertEqual(OfflineFixtureAdapter.calls, 1)

    async def test_polling_is_read_only_and_returns_completed_synthetic_run(self):
        accepted = await self.post_fetch()
        self.assertEqual(accepted.status_code, 202)
        fetch_id = accepted.json()["fetch_id"]
        for _ in range(200):
            response = await self.client.get(f"/api/v1/fetches/{fetch_id}")
            if response.json()["status"] in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED"}:
                break
            await asyncio.sleep(0.005)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "PARTIAL")
        self.assertEqual(response.json()["data_mode"], "SYNTHETIC")
        self.assertRegex(response.json()["run_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(OfflineFixtureAdapter.calls, 1)
        self.assertNotIn("HTTP 429", response.text)

    async def test_unknown_body_fields_and_oversized_body_are_rejected(self):
        unknown = await self.post_fetch(body='{"source_ids":["youtube_video_search"]}')
        oversized = await self.post_fetch(body='{"padding":"' + ("x" * 2048) + '"}')

        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(OfflineFixtureAdapter.calls, 0)

    async def test_origin_host_and_forwarded_headers_are_not_trusted(self):
        wrong_origin = await self.post_fetch(
            headers={**self.headers(), "Origin": "https://attacker.invalid"}
        )
        spoofed_forwarding = await self.post_fetch(
            headers={
                **self.headers("public-fetch-action-0002"),
                "X-Forwarded-Host": "attacker.invalid",
            }
        )
        wrong_host = await self.client.post(
            "/api/v1/fetches",
            content="{}",
            headers={
                "Host": "attacker.invalid",
                "Origin": "http://attacker.invalid",
                "Idempotency-Key": "public-fetch-action-0003",
                "Content-Type": "application/json",
            },
        )

        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(spoofed_forwarding.status_code, 403)
        self.assertEqual(wrong_host.status_code, 403)
        self.assertEqual(OfflineFixtureAdapter.calls, 0)

    async def test_legacy_run_route_still_requires_operator_bearer(self):
        denied = await self.client.post("/api/runs")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(OfflineFixtureAdapter.calls, 0)

    async def test_distributed_worker_facade_is_exposed_only_as_local_synthetic_fetch(self):
        state_path = str(Path(self.tmp.name) / "distributed.sqlite")
        # The API seam accepts only a fully declared four-source synthetic worker.
        # This fixture exercises the route and retained result projection;
        # source adapters are independently covered through MockTransport tests.
        service = distributed_fixture_service(state_path)
        app = create_app(
            settings=Settings.from_environment(TEST_ENV),
            lock_path=str(Path(self.tmp.name) / "distributed.lock"),
            distributed_public_fetch=service,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                capabilities = await client.get("/api/v1/capabilities")
                headers = {"Content-Type": "application/json", "Origin": "http://127.0.0.1",
                           "Idempotency-Key": "distributed-public-api-fixture-0001"}
                accepted = await client.post("/api/v1/fetches", content="{}", headers=headers)
                replay = await client.post("/api/v1/fetches", content="{}", headers=headers)
                fetch_id = accepted.json()["fetch_id"]
                status = await client.get(f"/api/v1/fetches/{fetch_id}")
                service.admission.disable()
                disabled = await client.get("/api/v1/capabilities")
                retained_status = await client.get(f"/api/v1/fetches/{fetch_id}")

        self.assertEqual(capabilities.status_code, 200)
        self.assertEqual(capabilities.json()["admission"]["guarantee_scope"],
                         "injected AtomicStateStore; backing guarantees are environment-specific")
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["fetch_id"], fetch_id)
        self.assertRegex(fetch_id, r"^[a-f0-9]{32}$")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["data_mode"], "SYNTHETIC")
        self.assertEqual({row["source_id"] for row in status.json()["sources"]},
                         _DISTRIBUTED_SOURCES)
        self.assertTrue(all(row["records"][0]["keyword"] == "synthetic fixture"
                            for row in status.json()["sources"]))
        self.assertFalse(disabled.json()["public_fetch_enabled"])
        self.assertEqual(disabled.json()["fetch_block_reason"], "FEATURE_DISABLED")
        self.assertEqual(retained_status.status_code, 200)
        self.assertNotIn("api_key", status.text.lower())
        self.assertNotIn("Authorization", capabilities.text)

    async def test_public_api_worker_quota_queue_and_shared_cache_run_together(self):
        state_path = str(Path(self.tmp.name) / "distributed-cache.sqlite")
        service, gateways, calls = distributed_cached_fixture_service(state_path)
        app = create_app(
            settings=Settings.from_environment(TEST_ENV),
            storage=FakeStorage(), adapter_factory=OfflineFixtureAdapter,
            lock_path=str(Path(self.tmp.name) / "distributed-cache.lock"),
            distributed_public_fetch=service,
        )
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
                ) as client:
                    first = await client.post(
                        "/api/v1/fetches", content="{}", headers={
                            "Content-Type": "application/json", "Origin": "http://127.0.0.1",
                            "Idempotency-Key": "combined-api-cache-fetch-0001",
                        },
                    )
                    self.assertEqual(first.status_code, 202)
                    first_status = await client.get(first.json()["status_url"])
                    self.assertEqual(first_status.status_code, 200, first_status.text)
                    self.assertEqual(first_status.json()["status"], "PARTIAL")
                    self.assertTrue(all(row["status"] in {"SUCCESS", "PARTIAL"}
                                        for row in first_status.json()["sources"]))
                    first_calls = dict(calls)
                    self.assertEqual(set(first_calls.values()), {1})

                    second = await client.post(
                        "/api/v1/fetches", content="{}", headers={
                            "Content-Type": "application/json", "Origin": "http://127.0.0.1",
                            "Idempotency-Key": "combined-api-cache-fetch-0002",
                        },
                    )
                    self.assertEqual(second.status_code, 202)
                    second_status = await client.get(second.json()["status_url"])
                    self.assertEqual(second_status.status_code, 200)
                    self.assertTrue(all(row["delivery_state"] == "CACHED"
                                        for row in second_status.json()["sources"]))
                    self.assertTrue(all(row["data_mode"] == "SYNTHETIC"
                                        for row in second_status.json()["sources"]))
                    self.assertEqual(calls, first_calls)
        finally:
            for gateway in gateways:
                await gateway.aclose()

    async def test_distributed_fetch_injection_cannot_enable_read_only_or_live(self):
        state_path = str(Path(self.tmp.name) / "read-only-distributed.sqlite")
        service = distributed_fixture_service(state_path)
        invalid_compositions = (
            (Settings.from_environment(TEST_ENV), {"read_only": True}),
            (Settings.from_environment({**TEST_ENV, "TREND_SCOUT_ENVIRONMENT": "production",
                                        "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false"}), {}),
            (Settings.from_environment({**TEST_ENV, "TREND_SCOUT_RUN_MODE": "live",
                                        "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false"}), {}),
            (Settings.from_environment({**TEST_ENV, "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false"}), {}),
        )
        for settings, options in invalid_compositions:
            with self.subTest(options=options, profile=settings.environment_profile,
                              run_mode=settings.run_mode), self.assertRaisesRegex(
                                  ValueError,
                                  "restricted to enabled local/test synthetic mode",
                              ):
                create_app(settings=settings, distributed_public_fetch=service, **options)

    async def test_public_rate_limit_counts_new_work_and_not_idempotent_replays(self):
        self.app.state.fetch_registry.max_fetches_per_window = 1
        first = await self.post_fetch()
        replay = await self.post_fetch()
        fetch_id = first.json()["fetch_id"]
        for _ in range(200):
            status = await self.client.get(f"/api/v1/fetches/{fetch_id}")
            if status.json()["status"] in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED"}:
                break
            await asyncio.sleep(0.005)
        limited = await self.post_fetch(key="public-fetch-action-0002")

        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(OfflineFixtureAdapter.calls, 1)

    async def test_run_cooldown_rejects_new_public_fetch_after_completion(self):
        settings = Settings.from_environment({**TEST_ENV, "TREND_SCOUT_RUN_COOLDOWN_SECONDS": "60"})
        app = create_app(
            settings=settings,
            storage=self.storage,
            adapter_factory=OfflineFixtureAdapter,
            lock_path=str(Path(self.tmp.name) / "cooldown.lock"),
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                headers = {
                    "Content-Type": "application/json",
                    "Origin": "http://127.0.0.1",
                    "Idempotency-Key": "public-fetch-cooldown-001",
                }
                accepted = await client.post("/api/v1/fetches", content="{}", headers=headers)
                fetch_id = accepted.json()["fetch_id"]
                for _ in range(200):
                    status = await client.get(f"/api/v1/fetches/{fetch_id}")
                    if status.json()["terminal"]:
                        break
                    await asyncio.sleep(0.005)
                limited = await client.post(
                    "/api/v1/fetches",
                    content="{}",
                    headers={**headers, "Idempotency-Key": "public-fetch-cooldown-002"},
                )

        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["detail"]["code"], "RUN_COOLDOWN_ACTIVE")
        self.assertEqual(limited.headers["retry-after"], "60")
        self.assertEqual(OfflineFixtureAdapter.calls, 1)

    async def test_default_configuration_disables_public_fetch_and_live_mode_fails_closed(self):
        default_app = create_app(settings=Settings.from_environment({}))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=default_app), base_url="http://127.0.0.1"
        ) as client:
            capabilities = (await client.get("/api/v1/capabilities")).json()
            blocked = await client.post(
                "/api/v1/fetches",
                content="{}",
                headers={
                    "Content-Type": "application/json",
                    "Origin": "http://127.0.0.1",
                    "Idempotency-Key": "public-fetch-action-0001",
                },
            )
        self.assertFalse(capabilities["public_fetch_enabled"])
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(OfflineFixtureAdapter.calls, 0)

        with self.assertRaises(ValueError):
            Settings.from_environment(
                {
                    **TEST_ENV,
                    "TREND_SCOUT_RUN_MODE": "live",
                }
            )

    async def test_unmarked_adapter_or_cloud_storage_cannot_enable_synthetic_fetch(self):
        settings = Settings.from_environment(TEST_ENV)
        app = create_app(
            settings=settings,
            storage=self.storage,
            adapter_factory=lambda: OfflineFixtureAdapter(),
            lock_path=str(Path(self.tmp.name) / "other.lock"),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            capability = (await client.get("/api/v1/capabilities")).json()
            response = await client.post(
                "/api/v1/fetches",
                content="{}",
                headers={
                    "Content-Type": "application/json",
                    "Origin": "http://127.0.0.1",
                    "Idempotency-Key": "public-fetch-action-0001",
                },
            )

        self.assertFalse(capability["public_fetch_enabled"])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(OfflineFixtureAdapter.calls, 0)

    async def test_second_process_guard_disables_public_fetch_for_shared_lock_path(self):
        second_app = create_app(
            settings=Settings.from_environment(TEST_ENV),
            storage=self.storage,
            adapter_factory=OfflineFixtureAdapter,
            lock_path=str(Path(self.tmp.name) / "run.lock"),
        )
        second_lifespan = second_app.router.lifespan_context(second_app)
        await second_lifespan.__aenter__()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second_app),
                base_url="http://127.0.0.1",
            ) as second_client:
                capability = (await second_client.get("/api/v1/capabilities")).json()
                response = await second_client.post(
                    "/api/v1/fetches",
                    content="{}",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "http://127.0.0.1",
                        "Idempotency-Key": "public-fetch-action-0002",
                    },
                )
            self.assertFalse(capability["public_fetch_enabled"])
            self.assertEqual(capability["fetch_block_reason"], "MULTI_PROCESS_RUNTIME_UNSUPPORTED")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(OfflineFixtureAdapter.calls, 0)
        finally:
            await second_lifespan.__aexit__(None, None, None)

    async def test_multi_worker_configuration_disables_public_fetch(self):
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "2"}, clear=False):
            app = create_app(
                settings=Settings.from_environment(TEST_ENV),
                storage=self.storage,
                adapter_factory=OfflineFixtureAdapter,
                lock_path=str(Path(self.tmp.name) / "multi-worker.lock"),
            )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                capability = (await client.get("/api/v1/capabilities")).json()

        self.assertFalse(capability["public_fetch_enabled"])
        self.assertEqual(capability["fetch_block_reason"], "MULTI_PROCESS_RUNTIME_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
