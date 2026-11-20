"""Vercel writes stay request-scoped and commit before success is returned."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx

from app.collections.firestore_snapshot import CollectionSnapshotUnavailable
from app.collections.service import CollectionService
from app.collections.store import CollectionStore
from app.main import (
    VercelSharedLiveMiddleware,
    _is_vercel_shared_live_request,
    create_app,
)
from app.settings import Settings

_AUDIENCE = (
    "//iam.googleapis.com/projects/556422620769/locations/global/"
    "workloadIdentityPools/trendscout-vercel/providers/vercel-production"
)


class _Client:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append("client_closed")


class VercelWritableTests(unittest.IsolatedAsyncioTestCase):
    def settings(self):
        return Settings.from_environment({
            "VERCEL": "1",
            "GOOGLE_CLOUD_PROJECT": "trendscout-192e4",
            "GOOGLE_WIF_AUDIENCE": _AUDIENCE,
            "TREND_SCOUT_READ_ONLY": "true",
            "TREND_SCOUT_VERCEL_SHARED_LIVE_ENABLED": "true",
        })

    async def test_collection_mutations_use_request_service_and_commit_before_response(self):
        settings = self.settings()
        self.assertFalse(settings.read_only)
        events = []
        service = object()
        snapshot = SimpleNamespace(save=lambda: events.append("snapshot_saved"))
        facade = SimpleNamespace(
            available_sources=set(), collection_service=service,
            collection_snapshot=snapshot,
        )
        client = _Client(events)

        @asynccontextmanager
        async def context_factory(_client, *, user_agent):
            self.assertTrue(user_agent)
            events.append("context_entered")
            try:
                yield facade
            finally:
                events.append("context_exited")

        async def app(scope, receive, send):
            self.assertIs(scope["state"]["collection_service"], service)
            await send({"type": "http.response.start", "status": 201, "headers": []})
            await send({"type": "http.response.body", "body": b"saved"})

        middleware = VercelSharedLiveMiddleware(
            app, settings=settings, enabled=True,
            client_factory=lambda _token: client,
            context_factory=context_factory,
        )
        scope = {
            "type": "http", "method": "POST", "path": "/api/v1/collections",
            "headers": [
                (b"host", b"trend-scout.example"),
                (b"origin", b"https://trend-scout.example"),
                (b"x-vercel-oidc-token", b"synthetic-test-token"),
            ],
            "state": {}, "scheme": "https", "query_string": b"",
            "server": ("trend-scout.example", 443),
        }
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = []

        async def send(message):
            if message["type"] == "http.response.start":
                events.append("response_started")
            sent.append(message)

        await middleware(scope, receive, send)
        self.assertEqual([message.get("status") for message in sent
                          if message["type"] == "http.response.start"], [201])
        self.assertLess(events.index("snapshot_saved"), events.index("response_started"))
        self.assertEqual(events[-2:], ["context_exited", "client_closed"])

    async def test_fetch_response_waits_for_background_work_and_snapshot_commit(self):
        settings = self.settings()
        events = []
        client = _Client(events)
        snapshot = SimpleNamespace(save=lambda: events.append("snapshot_saved"))
        facade = SimpleNamespace(
            available_sources=set(), collection_snapshot=snapshot,
        )

        @asynccontextmanager
        async def context_factory(_client, *, user_agent):
            yield facade

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 202, "headers": []})
            events.append("response_sent")
            await asyncio.sleep(0)
            events.append("background_finished")
            await send({"type": "http.response.body", "body": b"accepted"})

        middleware = VercelSharedLiveMiddleware(
            app, settings=settings, enabled=True,
            client_factory=lambda _token: client,
            context_factory=context_factory,
        )
        scope = {
            "type": "http", "method": "POST", "path": "/api/v1/fetches",
            "headers": [
                (b"host", b"trend-scout.example"),
                (b"origin", b"https://trend-scout.example"),
                (b"x-vercel-oidc-token", b"synthetic-test-token"),
            ],
            "state": {}, "scheme": "https", "query_string": b"",
            "server": ("trend-scout.example", 443),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                events.append("client_received_202")
            sent.append(message)

        sent = []
        await middleware(scope, receive, send)
        self.assertLess(events.index("background_finished"),
                        events.index("snapshot_saved"))
        self.assertLess(events.index("snapshot_saved"),
                        events.index("client_received_202"))
        self.assertEqual([message.get("status") for message in sent
                          if message["type"] == "http.response.start"], [202])

    async def test_fetch_snapshot_failure_returns_sanitized_persistence_error(self):
        settings = self.settings()
        events = []
        client = _Client(events)

        def fail_save():
            raise CollectionSnapshotUnavailable(
                "private-token-must-not-be-logged",
                failure_reason="BLOB_UPLOAD_FAILED",
                cause_class="PermissionDenied",
            )

        facade = SimpleNamespace(
            available_sources=set(), collection_snapshot=SimpleNamespace(save=fail_save),
        )

        @asynccontextmanager
        async def context_factory(_client, *, user_agent):
            yield facade

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b"accepted"})

        middleware = VercelSharedLiveMiddleware(
            app, settings=settings, enabled=True,
            client_factory=lambda _token: client,
            context_factory=context_factory,
        )
        scope = {
            "type": "http", "method": "POST", "path": "/api/v1/fetches",
            "headers": [
                (b"host", b"trend-scout.example"),
                (b"origin", b"https://trend-scout.example"),
                (b"x-vercel-oidc-token", b"synthetic-test-token"),
            ],
            "state": {}, "scheme": "https", "query_string": b"",
            "server": ("trend-scout.example", 443),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        with self.assertLogs("app.main", level="ERROR") as captured:
            await middleware(scope, receive, send)
        body = next(message["body"] for message in sent
                    if message["type"] == "http.response.body")
        self.assertIn(b"COLLECTIONS_PERSISTENCE_FAILED", body)
        self.assertTrue(any("failure_phase=COLLECTION_SNAPSHOT_COMMIT" in line
                            and "exception_class=CollectionSnapshotUnavailable" in line
                            and "failure_reason=BLOB_UPLOAD_FAILED" in line
                            and "cause_class=PermissionDenied" in line
                            and "confirmation_exception_class=None" in line
                            for line in captured.output))
        self.assertNotIn("private-token-must-not-be-logged", "\n".join(captured.output))

    def test_shared_middleware_routes_collection_reads_and_writes(self):
        settings = self.settings()
        self.assertTrue(_is_vercel_shared_live_request(
            settings, "GET", "/api/v1/collections", enabled=True,
        ))
        self.assertTrue(_is_vercel_shared_live_request(
            settings, "GET", "/api/v1/datasets/latest", enabled=True,
        ))
        self.assertTrue(_is_vercel_shared_live_request(
            settings, "GET", "/api/v1/datasets/latest/keywords", enabled=True,
        ))
        self.assertFalse(_is_vercel_shared_live_request(
            settings, "POST", "/api/v1/datasets/latest/keywords", enabled=True,
        ))
        self.assertTrue(_is_vercel_shared_live_request(
            settings, "POST", "/api/v1/collections", enabled=True,
        ))
        self.assertTrue(_is_vercel_shared_live_request(
            settings, "PATCH", "/api/v1/collections/topic-1", enabled=True,
        ))
        self.assertFalse(_is_vercel_shared_live_request(
            settings, "GET", "/api/v1/collections", enabled=False,
        ))
        self.assertFalse(_is_vercel_shared_live_request(
            settings, "GET", "/openapi.json", enabled=True,
        ))

    async def test_collections_api_uses_request_service_and_saves_mutations(self):
        settings = self.settings()
        events = []
        client = _Client(events)

        with TemporaryDirectory() as directory:
            collection_service = CollectionService(
                CollectionStore(Path(directory) / "collections.sqlite", data_mode_scope="LIVE")
            )
            snapshot = SimpleNamespace(save=lambda: events.append("snapshot_saved"))
            facade = SimpleNamespace(
                available_sources=set(), collection_service=collection_service,
                collection_snapshot=snapshot,
            )

            @asynccontextmanager
            async def context_factory(_client, *, user_agent):
                yield facade

            app = create_app(
                settings=settings,
                vercel_firestore_client_factory=lambda _token: client,
                vercel_live_context_factory=context_factory,
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://trend-scout.example",
                ) as browser:
                    headers = {
                        "Origin": "https://trend-scout.example",
                        "x-vercel-oidc-token": "synthetic-test-token",
                    }
                    created = await browser.post(
                        "/api/v1/collections", headers=headers,
                        json={"name": "Demo collection"},
                    )
                    listed = await browser.get("/api/v1/collections", headers=headers)
                    keyword_rows = await browser.get(
                        "/api/v1/datasets/latest/keywords?limit=1", headers=headers,
                    )

        self.assertEqual(created.status_code, 201, created.text)
        self.assertIn("snapshot_saved", events)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertIn("Demo collection",
                      [item["name"] for item in listed.json()["items"]])
        self.assertEqual(keyword_rows.status_code, 200, keyword_rows.text)
        self.assertEqual(keyword_rows.json()["total"], 0)


if __name__ == "__main__":
    unittest.main()
