"""Vercel Wikimedia Fetch gate and request-scoped worker lifetime contracts."""

import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from app.collections.service import CollectionService
from app.collections.store import CollectionStore
from app.jobs.public_admission import AdmissionDenied
from app.main import _is_vercel_shared_live_request, _persists_vercel_collections, create_app
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)

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
        self.submitted_keys = []
        self.status_calls = 0
        self.collection_service = None
        self.submit_status = "QUEUED"
        self.force_replay = False
        self.slice_state = "PARTIAL"
        self.slice_calls = []

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

    def submit(self, idempotency_key, *, source_ids):
        self.submitted_keys.append(idempotency_key)
        self.submitted_sources = source_ids
        return {
            "job_id": _JOB_ID,
            "status": self.submit_status,
            "result": "REPLAY" if self.force_replay or len(self.submitted_keys) > 1 else "CREATED",
            "created_at": "2026-11-19T00:00:00+00:00",
        }

    async def run_once(self, _owner):
        entered = max((index for index, event in enumerate(self.events)
                       if event == "context_entered"), default=-1)
        exited = max((index for index, event in enumerate(self.events)
                      if event == "context_exited"), default=-1)
        if self.client.closed or entered < exited:
            raise AssertionError("request resources closed before the background worker finished")
        self.events.append("worker_finished")
        return None

    async def run_slice(self, _owner, fetch_id, *, max_sources):
        entered = max((index for index, event in enumerate(self.events)
                       if event == "context_entered"), default=-1)
        exited = max((index for index, event in enumerate(self.events)
                      if event == "context_exited"), default=-1)
        if self.client.closed or entered < exited:
            raise AssertionError("request resources closed before the source slice finished")
        self.slice_calls.append((fetch_id, max_sources))
        self.events.append("worker_finished")
        return {"job_id": fetch_id, "state": self.slice_state, "sources": {}}

    def status(self, _fetch_id, *, include_records=False):
        self.status_calls += 1
        result = self._history_item()
        result["terminal"] = True
        result["selected_sources"] = [WIKIMEDIA_SOURCE_ID]
        if include_records:
            result["sources"] = [{
                "source_id": WIKIMEDIA_SOURCE_ID,
                "data_mode": "LIVE",
                "status": "SUCCESS",
                "records": [{"title": "retained checkpoint record"}],
                "record_count": 1,
            }]
        return result

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
        facade.collection_snapshot = SimpleNamespace(
            save=lambda: events.append("snapshot_saved")
        )
        client_factory = Mock(return_value=client)

        @asynccontextmanager
        async def context_factory(_client, *, user_agent):
            self.assertTrue(user_agent)
            facade.client = _client
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

    async def test_first_vercel_fetch_post_returns_before_running_source_slice(self):
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
        self.assertEqual(events, ["context_entered", "context_exited", "client_closed"])
        self.assertEqual(facade.slice_calls, [])
        client_factory.assert_called_once_with("synthetic-test-token")
        self.assertTrue(client.closed)

    async def test_all_four_approved_sources_can_be_submitted_together(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        sources = [
            WIKIMEDIA_SOURCE_ID,
            GOOGLE_SOURCE_ID,
            BING_SOURCE_ID,
            YOUTUBE_SOURCE_ID,
        ]
        facade.available_sources = frozenset(sources)
        facade.admission.policy.enabled_sources = facade.available_sources
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-four-sources-test-0001",
                    },
                    json={"sources": sources},
                )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(set(response.json()["sources"]), set(sources))
        self.assertEqual(facade.submitted_sources, sorted(sources))
        self.assertNotIn("worker_finished", events)
        self.assertEqual(facade.slice_calls, [])

    async def test_nonterminal_source_slice_skips_collection_snapshot_save(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        _client_factory.side_effect = lambda _token: _Client(events)
        facade.slice_state = "QUEUED"
        headers = {
            "Origin": "https://trend-scout.example",
            "x-vercel-oidc-token": "synthetic-test-token",
            "Idempotency-Key": "vercel-source-slice-test-0001",
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                accepted = await browser.post("/api/v1/fetches", headers=headers, json={})
                response = await browser.post("/api/v1/fetches", headers=headers, json={})

        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(facade.slice_calls, [(_JOB_ID, 1)])
        self.assertNotIn("collections_processed", events)
        self.assertNotIn("snapshot_saved", events)
        self.assertEqual(events.count("worker_finished"), 1)
        last_context_exit = max(index for index, event in enumerate(events)
                                if event == "context_exited")
        self.assertLess(events.index("worker_finished"), last_context_exit)

    async def test_idempotent_recovery_post_dispatches_a_new_vercel_worker(self):
        app, events, _client, facade, client_factory = self.app(enabled=True)
        client_factory.side_effect = lambda _token: _Client(events)
        sources = [WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID]
        facade.available_sources = frozenset(sources)
        facade.admission.policy.enabled_sources = facade.available_sources
        headers = {
            "Origin": "https://trend-scout.example",
            "x-vercel-oidc-token": "synthetic-test-token",
            "Idempotency-Key": "vercel-recovery-test-0001",
            "X-YouTube-Policy-Version": "test-policy-v1",
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                first = await browser.post(
                    "/api/v1/fetches", headers=headers, json={"sources": sources},
                )
                resumed = await browser.post(
                    "/api/v1/fetches", headers=headers, json={"sources": sources},
                )

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(resumed.status_code, 202, resumed.text)
        self.assertEqual(first.json()["fetch_id"], resumed.json()["fetch_id"])
        self.assertEqual(resumed.headers["idempotent-replay"], "true")
        self.assertEqual(facade.submitted_keys, [headers["Idempotency-Key"]] * 2)
        self.assertEqual(events.count("worker_finished"), 1)
        self.assertEqual(events.count("context_entered"), 2)
        self.assertEqual(events.count("context_exited"), 2)

    async def test_feature_gate_off_never_constructs_identity_or_worker(self):
        app, events, _client, _facade, client_factory = self.app(enabled=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.post("/api/v1/fetches", json={})

        self.assertEqual(response.status_code, 503)
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

    def test_checkpoint_recovery_uses_shared_live_middleware(self):
        settings = self.settings(enabled=True)
        self.assertTrue(_is_vercel_shared_live_request(
            settings, "POST", f"/api/v1/fetches/{_JOB_ID}/collections", enabled=True,
        ))
        self.assertFalse(_is_vercel_shared_live_request(
            settings, "POST", "/api/v1/fetches/not-a-fetch-id/collections", enabled=True,
        ))

    def test_research_routes_use_shared_live_middleware(self):
        settings = self.settings(enabled=True)
        for method, path in (
            ("GET", "/api/v1/research"),
            ("POST", "/api/v1/research"),
            ("PATCH", "/api/v1/research/research-id"),
        ):
            with self.subTest(method=method, path=path):
                self.assertTrue(_is_vercel_shared_live_request(
                    settings, method, path, enabled=True,
                ))
        self.assertTrue(_persists_vercel_collections("POST", "/api/v1/research"))
        self.assertTrue(_persists_vercel_collections(
            "PATCH", "/api/v1/research/research-id",
        ))

    async def test_research_reads_and_mutations_use_and_persist_request_store(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        with TemporaryDirectory() as directory:
            facade.collection_service = CollectionService(CollectionStore(
                Path(directory) / "collections.sqlite", data_mode_scope="LIVE",
            ))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://trend-scout.example",
                ) as browser:
                    headers = {"x-vercel-oidc-token": "synthetic-test-token"}
                    before = await browser.get("/api/v1/research", headers=headers)
                    created = await browser.post(
                        "/api/v1/research",
                        headers={**headers, "Origin": "https://trend-scout.example"},
                        json={"subject": "Fixture", "hypothesis": "A testable idea."},
                    )
                    updated = await browser.patch(
                        f"/api/v1/research/{created.json()['id']}",
                        headers={**headers, "Origin": "https://trend-scout.example"},
                        json={"status": "RESEARCHING"},
                    )
                    after = await browser.get("/api/v1/research", headers=headers)

        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(after.status_code, 200, after.text)
        self.assertEqual(after.json()["total"], 1)
        self.assertEqual(after.json()["items"][0]["status"], "RESEARCHING")
        self.assertEqual(events.count("snapshot_saved"), 2)

    async def test_bing_only_request_adds_google_dependency_before_admission(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        facade.available_sources = frozenset({
            WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
        })
        facade.admission.policy.enabled_sources = facade.available_sources
        facade.data_mode = "LIVE"
        facade.normal_bing_live = True
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-bing-auto-google-0001",
                    },
                    json={"sources": [BING_SOURCE_ID]},
                )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["requested_sources"], [BING_SOURCE_ID])
        self.assertEqual(
            response.json()["sources"], sorted([GOOGLE_SOURCE_ID, BING_SOURCE_ID])
        )
        self.assertEqual(facade.submitted_sources,
                         sorted([GOOGLE_SOURCE_ID, BING_SOURCE_ID]))

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
        self.assertFalse(capabilities.json()["read_only"])
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

    async def test_existing_terminal_fetch_replays_collections_without_running_worker(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        facade.submit_status = "PARTIAL"
        facade.force_replay = True
        processed = []

        class _Service:
            store = SimpleNamespace(data_mode_scope="LIVE")

            async def process_fetch(self, fetch_id, sources, *, cached_context_only=False):
                processed.append((fetch_id, sources, cached_context_only))
                events.append("collections_processed")
                return {"status": "COMPLETED"}

        facade.collection_service = _Service()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-terminal-postprocess-0001",
                    },
                    json={},
                )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "PARTIAL")
        self.assertEqual(processed[0][0], _JOB_ID)
        self.assertTrue(processed[0][2])
        self.assertNotIn("worker_finished", events)
        self.assertLess(events.index("collections_processed"), events.index("snapshot_saved"))

    async def test_checkpoint_recovery_endpoint_never_runs_source_worker(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        processed = []

        class _Service:
            store = SimpleNamespace(data_mode_scope="LIVE")

            async def process_fetch(self, fetch_id, sources, *, cached_context_only=False):
                processed.append((fetch_id, sources, cached_context_only))
                events.append("collections_processed")
                return {"status": "COMPLETED"}

        facade.collection_service = _Service()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    f"/api/v1/fetches/{_JOB_ID}/collections",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "COMPLETED")
        self.assertEqual(processed[0][0], _JOB_ID)
        self.assertEqual(processed[0][1][0]["records"][0]["title"],
                         "retained checkpoint record")
        self.assertTrue(processed[0][2])
        self.assertNotIn("worker_finished", events)
        self.assertLess(events.index("collections_processed"), events.index("snapshot_saved"))

    async def test_expired_checkpoints_replay_only_retained_live_caches(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        source_ids = [WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID]
        facade.available_sources = frozenset(source_ids)
        facade.admission.policy.enabled_sources = facade.available_sources
        old_manifest = [
            {"source_id": WIKIMEDIA_SOURCE_ID, "status": "SUCCESS", "record_count": 1,
             "delivery_state": "FRESH", "coverage": {}},
            {"source_id": GOOGLE_SOURCE_ID, "status": "UNAVAILABLE", "record_count": 0,
             "error_code": "NOT_YET_ELIGIBLE", "coverage": {}},
            {"source_id": BING_SOURCE_ID, "status": "UNAVAILABLE", "record_count": 0,
             "error_code": "NO_SAVED_CANDIDATE", "coverage": {}},
            {"source_id": YOUTUBE_SOURCE_ID, "status": "PARTIAL", "record_count": 1,
             "delivery_state": "FRESH", "coverage": {}},
        ]
        facade.history = lambda limit=20: [{
            **facade._history_item(), "source_manifest": old_manifest,
        }][:limit]
        facade.status = lambda _fetch_id, include_records=False: {
            **facade._history_item(),
            "terminal": True,
            "data_mode": None,
            "selected_sources": source_ids,
            "sources": [{
                "source_id": source_id,
                "status": "UNAVAILABLE",
                "data_mode": "LIVE",
                "record_count": 0,
                "records": [],
                "error_code": "RESULT_UNAVAILABLE",
                "coverage": {"checkpoint_status": "RESULT_UNAVAILABLE"},
            } for source_id in source_ids],
        }
        cache_reads = []
        cached_results = {
            WIKIMEDIA_SOURCE_ID: {
                "source_id": WIKIMEDIA_SOURCE_ID, "status": "SUCCESS", "data_mode": "LIVE",
                "delivery_state": "CACHED", "record_count": 1,
                "records": [{"title": "retained Wikimedia cache"}],
                "provider_request_count": 0,
                "coverage": {"cached_provider_request_count": 1},
            },
            YOUTUBE_SOURCE_ID: {
                "source_id": YOUTUBE_SOURCE_ID, "status": "PARTIAL", "data_mode": "LIVE",
                "delivery_state": "STALE_CACHE", "record_count": 1,
                "records": [{"video_id": "retained YouTube cache"}],
                "provider_request_count": 0,
                "coverage": {"cached_provider_request_count": 1},
            },
        }

        def read_cached_source_result(source_id):
            cache_reads.append(source_id)
            return cached_results.get(source_id)

        facade.read_cached_source_result = read_cached_source_result
        processed = []

        class _Service:
            store = SimpleNamespace(data_mode_scope="LIVE")

            async def process_fetch(self, fetch_id, sources, *, cached_context_only=False):
                processed.append((fetch_id, sources, cached_context_only))
                events.append("collections_processed")
                return {"status": "COMPLETED"}

        facade.collection_service = _Service()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://trend-scout.example",
            ) as browser:
                response = await browser.post(
                    f"/api/v1/fetches/{_JOB_ID}/collections",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "COMPLETED")
        self.assertEqual(processed[0][0], _JOB_ID)
        self.assertTrue(processed[0][2])
        self.assertEqual(set(cache_reads), set(source_ids))
        restored = {source["source_id"]: source for source in processed[0][1]}
        self.assertEqual(set(restored), set(source_ids))
        self.assertEqual(restored[WIKIMEDIA_SOURCE_ID]["records"][0]["title"],
                         "retained Wikimedia cache")
        self.assertEqual(restored[WIKIMEDIA_SOURCE_ID]["delivery_state"], "CACHED")
        self.assertEqual(restored[YOUTUBE_SOURCE_ID]["delivery_state"], "STALE_CACHE")
        self.assertEqual(restored[GOOGLE_SOURCE_ID]["error_code"], "NOT_YET_ELIGIBLE")
        self.assertEqual(restored[BING_SOURCE_ID]["error_code"], "NO_SAVED_CANDIDATE")
        self.assertTrue(all(source["data_mode"] == "LIVE" for source in restored.values()))
        self.assertNotIn("worker_finished", events)
        self.assertLess(events.index("collections_processed"), events.index("snapshot_saved"))

    async def test_recognized_policy_mismatch_exposes_explicit_sync_then_fetch(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)
        original_submit = facade.submit

        def submit_after_sync(idempotency_key, *, source_ids):
            events.append("fetch_submitted")
            return original_submit(idempotency_key, source_ids=source_ids)

        def prepare_for_fetch():
            events.append("policy_reconciled")
            return "RECONCILED"

        facade.submit = submit_after_sync
        facade.prepare_for_wikimedia_fetch = prepare_for_fetch
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                headers = {"x-vercel-oidc-token": "synthetic-test-token"}
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={**headers, "Origin": "https://trend-scout.example",
                             "Idempotency-Key": "vercel-wikimedia-sync-0001"},
                    json={},
                )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["shared_state_policy_sync"], "RECONCILED")
        self.assertLess(events.index("policy_reconciled"), events.index("fetch_submitted"))
        self.assertNotIn("worker_finished", events)

    async def test_recognized_policy_mismatch_is_actionable_in_capabilities(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        facade.admission.operationally_enabled = lambda: False
        facade.vercel_wikimedia_policy_sync_status = lambda: "READY_TO_SYNC"
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.get(
                    "/api/v1/capabilities",
                    headers={"x-vercel-oidc-token": "synthetic-test-token"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["public_fetch_enabled"])
        self.assertTrue(response.json()["shared_policy_reconciliation_required"])
        self.assertTrue(response.json()["vercel_shared_live"])

    async def test_policy_sync_failure_stops_before_job_admission_or_provider_worker(self):
        app, events, _client, facade, _client_factory = self.app(enabled=True)

        def reject_policy_sync():
            raise AdmissionDenied("POLICY_MISMATCH")

        facade.prepare_for_wikimedia_fetch = reject_policy_sync
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.post(
                    "/api/v1/fetches",
                    headers={
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                        "Idempotency-Key": "vercel-wikimedia-sync-failed-0001",
                    },
                    json={},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "POLICY_MISMATCH")
        self.assertIsNone(facade.submitted_sources)
        self.assertNotIn("worker_finished", events)

    async def test_unknown_policy_mismatch_stays_disabled(self):
        app, _events, _client, facade, _client_factory = self.app(enabled=True)
        facade.admission.operationally_enabled = lambda: False
        facade.vercel_wikimedia_policy_sync_status = lambda: "POLICY_MISMATCH"
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://trend-scout.example"
            ) as browser:
                response = await browser.get(
                    "/api/v1/capabilities",
                    headers={"x-vercel-oidc-token": "synthetic-test-token"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["public_fetch_enabled"])
        # Runtime identity drives UI wording even when policy currently blocks
        # new admissions; Vercel should never be described as local LIVE.
        self.assertTrue(response.json()["vercel_shared_live"])
        self.assertFalse(response.json()["shared_policy_reconciliation_required"])
        self.assertEqual(response.json()["fetch_block_reason"], "POLICY_MISMATCH")


if __name__ == "__main__":
    unittest.main()
