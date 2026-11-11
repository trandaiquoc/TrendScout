"""Public Fetch admission stays bounded and offline-only until live approval."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from test_runs import fixture

from app.main import create_app
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.storage.fake import FakeStorage

TEST_ENV = {
    "TREND_SCOUT_ENVIRONMENT": "test",
    "TREND_SCOUT_RUN_MODE": "synthetic",
    "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true",
    "TREND_SCOUT_RUN_COOLDOWN_SECONDS": "0",
    "TREND_SCOUT_OPERATOR_TOKEN": "test-only-legacy-token-123456789",
}


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
        for source_id in (YOUTUBE_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID):
            self.assertEqual(sources[source_id]["approval_status"], "PENDING_APPROVAL")
            self.assertFalse(sources[source_id]["enabled_for_current_operation"])
        self.assertNotIn("Authorization", response.text)
        self.assertNotIn("password", response.text.lower())
        self.assertNotIn("api_key", response.text.lower())

    async def test_same_key_concurrent_posts_return_one_execution(self):
        first, second = await asyncio.gather(
            self.post_fetch(), self.post_fetch()
        )

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
            headers={**self.headers("public-fetch-action-0002"), "X-Forwarded-Host": "attacker.invalid"}
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
        settings = Settings.from_environment(
            {**TEST_ENV, "TREND_SCOUT_RUN_COOLDOWN_SECONDS": "60"}
        )
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
            self.assertEqual(
                capability["fetch_block_reason"], "MULTI_PROCESS_RUNTIME_UNSUPPORTED"
            )
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
        self.assertEqual(
            capability["fetch_block_reason"], "MULTI_PROCESS_RUNTIME_UNSUPPORTED"
        )


if __name__ == "__main__":
    unittest.main()
