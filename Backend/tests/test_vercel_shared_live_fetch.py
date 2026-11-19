"""Vercel Wikimedia Fetch gate and request-scoped worker lifetime contracts."""

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from app.main import create_app
from app.settings import Settings
from app.sources.catalog import WIKIMEDIA_SOURCE_ID

_AUDIENCE = (
    "//iam.googleapis.com/projects/556422620769/locations/global/"
    "workloadIdentityPools/trendscout-vercel/providers/vercel-production"
)
_PROJECT = "trendscout-192e4"
_JOB_ID = "a" * 32


class _Client:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def close(self):
        self.closed = True
        self.events.append("client_closed")


class _FetchFacade:
    def __init__(self, events, client):
        self.events = events
        self.client = client
        self.available_sources = frozenset({WIKIMEDIA_SOURCE_ID})
        self.data_mode = "LIVE"
        self.firestore_shared_live = True
        self.vercel_wikimedia_live = True
        self.admission = SimpleNamespace(
            policy=SimpleNamespace(
                enabled_sources=self.available_sources,
                max_starts_per_window=3,
                window_seconds=3600,
                cooldown_seconds=60,
                max_status_polls=120,
                receipt_seconds=3600,
            ),
            operationally_enabled=lambda: True,
        )
        self.submitted_sources = None
        self.status_calls = 0

    @staticmethod
    def _history_item():
        return {
            "fetch_id": _JOB_ID,
            "run_type": "FETCH",
            "status": "COMPLETED",
            "outcome_status": "COMPLETED",
            "data_mode": "LIVE",
            "mode": "LIVE",
            "sources": [WIKIMEDIA_SOURCE_ID],
            "source_manifest": [{
                "source_id": WIKIMEDIA_SOURCE_ID,
                "status": "SUCCESS",
                "record_count": 1,
                "delivery_state": "FRESH",
                "error_code": None,
                "coverage": {},
            }],
            "received_record_count": 1,
            "retained_keyword_count": None,
            "rejected_record_count": None,
            "created_at": "2026-11-19T00:00:00+00:00",
            "updated_at": "2026-11-19T00:00:01+00:00",
            "run_at": "2026-11-19T00:00:01+00:00",
            "fetched_at": "2026-11-19T00:00:01+00:00",
            "duration_seconds": None,
            "progress": {"completed_sources": 1, "total_sources": 1},
        }

    def submit(self, _idempotency_key, *, source_ids):
        self.submitted_sources = source_ids
        return {
            "job_id": _JOB_ID,
            "status": "QUEUED",
            "result": "CREATED",
            "created_at": "2026-11-19T00:00:00+00:00",
        }

    async def run_once(self, _owner):
        if self.client.closed or "context_exited" in self.events:
            raise AssertionError("request resources closed before the background worker finished")
        self.events.append("worker_finished")
        return None

    def status(self, _fetch_id):
        self.status_calls += 1
        return self._history_item()

    def history(self, *, limit=20):
        return [self._history_item()][:limit]


class VercelSharedLiveFetchTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, *, enabled):
        values = {
            "VERCEL": "1",
            "GOOGLE_CLOUD_PROJECT": _PROJECT,
            "GOOGLE_WIF_AUDIENCE": _AUDIENCE,
            "TREND_SCOUT_VERCEL_SHARED_LIVE_ENABLED": "true" if enabled else "false",
            "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
        }
        return Settings.from_environment(values)

    def app(self, *, enabled):
        events = []
        client = _Client(events)
        facade = _FetchFacade(events, client)
        client_factory = Mock(return_value=client)

        @asynccontextmanager
        async def context_factory(_client, *, user_agent):
            self.assertTrue(user_agent)
            events.append("context_entered")
            try:
                yield facade
            finally:
                events.append("context_exited")

        application = create_app(
            settings=self.settings(enabled=enabled),
            vercel_firestore_client_factory=client_factory,
            vercel_live_context_factory=context_factory,
        )
        return application, events, client, facade, client_factory

    async def test_enabled_wikimedia_fetch_keeps_worker_and_firestore_alive(self):
        app, events, client, facade, client_factory = self.app(enabled=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-wikimedia-test-0001",
                    },
                    json={},
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["sources"], [WIKIMEDIA_SOURCE_ID])
        self.assertEqual(facade.submitted_sources, [WIKIMEDIA_SOURCE_ID])
        self.assertEqual(events, [
            "context_entered", "worker_finished", "context_exited", "client_closed",
        ])
        client_factory.assert_called_once_with("synthetic-test-token")
        self.assertTrue(client.closed)

    async def test_feature_gate_off_never_constructs_identity_or_worker(self):
        app, events, _client, _facade, client_factory = self.app(enabled=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.post("/api/v1/fetches", json={})

        self.assertEqual(response.status_code, 405)
        client_factory.assert_not_called()
        self.assertEqual(events, [])

    async def test_missing_or_cross_origin_identity_fails_before_firestore(self):
        app, _events, _client, _facade, client_factory = self.app(enabled=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                missing_token = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "Idempotency-Key": "vercel-wikimedia-test-0002",
                    },
                    json={},
                )
                cross_origin = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://attacker.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-wikimedia-test-0003",
                    },
                    json={},
                )

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(cross_origin.status_code, 403)
        client_factory.assert_not_called()

    async def test_other_providers_are_not_selectable_in_wikimedia_slice(self):
        app, _events, _client, _facade, _client_factory = self.app(enabled=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-wikimedia-test-0004",
                    },
                    json={"sources": ["google_web_serp"]},
                )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn("worker_finished", _events)

    async def test_shared_dashboard_history_and_detail_are_available_without_fabricated_totals(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                headers = {"x-vercel-oidc-token": "synthetic-test-token"}
                history = await browser.get("/api/v1/runs/history", headers=headers)
                capabilities = await browser.get("/api/v1/capabilities", headers=headers)
                detail = await browser.get(
                    f"/api/v1/runs/history/{_JOB_ID}", headers=headers
                )
                overview = await browser.get(
                    "/api/v1/analytics/overview", headers=headers
                )

        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["history_source"], "local_sqlite_and_shared_firestore")
        self.assertEqual(history.json()["items"][0]["fetch_id"], _JOB_ID)
        self.assertEqual(capabilities.status_code, 200, capabilities.text)
        self.assertTrue(capabilities.json()["vercel_shared_live"])
        self.assertTrue(capabilities.json()["read_only"])
        self.assertEqual(capabilities.json()["data_mode"], "LIVE")
        self.assertEqual(
            [source["source_id"] for source in capabilities.json()["sources"]
             if source["enabled_for_current_operation"]],
            [WIKIMEDIA_SOURCE_ID],
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["item"]["received_record_count"], 1)
        self.assertEqual(facade.status_calls, 0)
        self.assertEqual(overview.status_code, 200, overview.text)
        self.assertIsNone(overview.json()["keyword_totals"]["unique"])
        self.assertEqual(overview.json()["shared_fetch_history"]["items"], 1)


if __name__ == "__main__":
    unittest.main()
