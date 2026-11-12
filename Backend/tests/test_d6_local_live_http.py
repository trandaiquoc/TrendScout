"""Guarded local LIVE HTTP integration using only fixture handlers."""

import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.local_smoke_policy import local_smoke_policy
from app.main import create_local_live_app
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.settings import Settings
from app.storage.local_smoke_store import LocalSmokeStore
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_volatile_checkpoints import LocalVolatileCheckpoints


NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


def _live_facade(directory):
    backing = LocalSQLiteAtomicStateStore.for_local_live(Path(directory) / "quota.sqlite")
    checkpoints = LocalVolatileCheckpoints(backing, clock=lambda: NOW)
    policy = replace(local_smoke_policy(), enabled=True)
    admission = PublicAdmission(LocalSmokeStore(checkpoints), policy, state_key="http-fixture")

    def grant(source, now):
        return CheckpointGrant(source, "LIVE", "fixture-retention", now + timedelta(minutes=1),
                               30, True)

    attempts = {"count": 0}

    def handler(_context):
        attempts["count"] += 1
        return {
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "record_count": 1,
            "records": [{"raw": "fixture-private-content"}],
        }

    handler.__trendscout_data_mode__ = "LIVE"
    failed_source = sorted(policy.enabled_sources)[0]

    def unavailable_handler(context):
        attempts["count"] += 1
        return {"status": "FAILED", "data_mode": "LIVE", "record_count": 0,
                "records": [], "error_code": "FIXTURE_FAILURE"}

    unavailable_handler.__trendscout_data_mode__ = "LIVE"
    handlers = {source: (unavailable_handler if source == failed_source else handler)
                for source in policy.enabled_sources}
    worker = DistributedFetchWorker(admission, handlers=handlers, resolve_grant=grant,
                                    utcnow=lambda: NOW)
    facade = DistributedPublicFetch(admission, worker, resolve_grant=grant,
                                    utcnow=lambda: NOW, local_live=True,
                                    resolve_processing_grants=lambda _now: ())
    return facade, checkpoints, attempts


class LocalLiveHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_guarded_http_contract_idempotency_status_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            facade, checkpoints, attempts = _live_facade(directory)
            lifecycle = {"entered": 0, "exited": 0}

            @asynccontextmanager
            async def composition():
                lifecycle["entered"] += 1
                try:
                    yield facade
                finally:
                    lifecycle["exited"] += 1
                    checkpoints.close()

            app = create_local_live_app(
                enabled=True, composition_factory=composition,
                settings=Settings.from_environment({"TREND_SCOUT_ENVIRONMENT": "local"}),
            )
            self.assertEqual(lifecycle, {"entered": 0, "exited": 0})
            inner = app.app
            async with inner.router.lifespan_context(inner):
                self.assertEqual(attempts["count"], 0)
                transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://127.0.0.1") as client:
                    capabilities = await client.get("/api/v1/capabilities")
                    self.assertEqual(capabilities.status_code, 200)
                    self.assertTrue(capabilities.json()["local_live_demo"])
                    self.assertEqual(capabilities.json()["data_mode"], "LIVE")

                    headers = {"Idempotency-Key": "http-fixture-request-0001"}
                    first = await client.post("/api/v1/fetches", json={}, headers=headers)
                    replay = await client.post("/api/v1/fetches", json={}, headers=headers)
                    self.assertEqual(first.status_code, 202)
                    self.assertEqual(replay.status_code, 202)
                    self.assertNotEqual(first.json()["fetch_id"], "")
                    self.assertEqual(first.json()["fetch_id"], replay.json()["fetch_id"])
                    self.assertEqual(replay.headers["Idempotent-Replay"], "true")

                    status = await client.get(first.json()["status_url"])
                    self.assertEqual(status.status_code, 200)
                    self.assertEqual(status.json()["status"], "PARTIAL")
                    self.assertEqual(status.json()["data_mode"], "LIVE")
                    self.assertEqual(status.json()["discovery"]["candidate_count"], 0)
                    source_results = status.json()["sources"]
                    self.assertEqual(sum(item["status"] == "FAILED" for item in source_results), 1)
                    self.assertEqual(sum(item["status"] == "SUCCESS" for item in source_results), 3)
                    self.assertTrue(all(item["delivery_state"] == "FRESH"
                                        for item in source_results))
                    self.assertNotIn("fixture-private-content", status.text)
                    again = await client.get(first.json()["status_url"])
                    self.assertEqual(again.json()["status"], status.json()["status"])
                    self.assertEqual(attempts["count"], 4)
            self.assertEqual(lifecycle, {"entered": 1, "exited": 1})

    async def test_loopback_host_proxy_origin_method_and_payload_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            facade, checkpoints, _attempts = _live_facade(directory)

            @asynccontextmanager
            async def composition():
                try:
                    yield facade
                finally:
                    checkpoints.close()

            app = create_local_live_app(
                enabled=True, composition_factory=composition,
                settings=Settings.from_environment({"TREND_SCOUT_ENVIRONMENT": "local"}),
            )
            inner = app.app
            async with inner.router.lifespan_context(inner):
                cases = (
                    ("http://127.0.0.1", "GET", "/api/v1/capabilities",
                     {"Host": "evil.example"}, "127.0.0.1", 403),
                    ("http://127.0.0.1", "GET", "/api/v1/capabilities",
                     {"X-Forwarded-For": "127.0.0.1"}, "127.0.0.1", 403),
                    ("http://127.0.0.1", "GET", "/api/v1/capabilities",
                     {"X-Forwarded-Prefix": "/api"}, "127.0.0.1", 403),
                    ("http://127.0.0.1", "GET", "/api/v1/capabilities",
                     {"Origin": "http://evil.example"}, "127.0.0.1", 403),
                    ("https://127.0.0.1", "GET", "/api/v1/capabilities",
                     {}, "127.0.0.1", 403),
                    ("http://127.0.0.1", "PUT", "/api/v1/fetches",
                     {}, "127.0.0.1", 405),
                )
                for base_url, method, path, headers, peer, expected in cases:
                    transport = httpx.ASGITransport(app=app, client=(peer, 1234))
                    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
                        response = await client.request(method, path, headers=headers)
                    self.assertEqual(response.status_code, expected)

                transport = httpx.ASGITransport(app=app, client=("192.0.2.9", 1234))
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://127.0.0.1") as client:
                    response = await client.get("/api/v1/capabilities")
                    self.assertEqual(response.status_code, 403)

                transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://127.0.0.1") as client:
                    bad_payload = await client.post(
                        "/api/v1/fetches",
                        json={"url": "bad"},
                        headers={"Idempotency-Key": "payload-case-0001"},
                    )
                    self.assertEqual(bad_payload.status_code, 422)

    async def test_opt_in_local_profile_vercel_and_workers_fail_closed(self):
        with self.assertRaises(ValueError):
            create_local_live_app()

        @asynccontextmanager
        async def empty_composition():
            yield None

        settings = Settings.from_environment({"TREND_SCOUT_ENVIRONMENT": "test"})
        with self.assertRaises(ValueError):
            create_local_live_app(enabled=True, composition_factory=empty_composition,
                                  settings=settings)
        synthetic_settings = Settings.from_environment({
            "TREND_SCOUT_ENVIRONMENT": "local",
            "TREND_SCOUT_RUN_MODE": "synthetic",
        })
        with self.assertRaises(ValueError):
            create_local_live_app(enabled=True, composition_factory=empty_composition,
                                  settings=synthetic_settings)
        with patch.dict("os.environ", {"VERCEL": "1"}):
            with self.assertRaises(ValueError):
                create_local_live_app(enabled=True, composition_factory=empty_composition,
                    settings=Settings.from_environment({"TREND_SCOUT_ENVIRONMENT": "local"}))
        with patch.dict("os.environ", {"UVICORN_WORKERS": "2"}):
            with self.assertRaises(ValueError):
                create_local_live_app(enabled=True, composition_factory=empty_composition,
                    settings=Settings.from_environment({"TREND_SCOUT_ENVIRONMENT": "local"}))
        with self.assertRaises(ValueError):
            create_local_live_app(enabled=True, workers=2, composition_factory=empty_composition,
                settings=Settings.from_environment({"TREND_SCOUT_ENVIRONMENT": "local"}))


if __name__ == "__main__":
    unittest.main()
