import unittest

import httpx
from fastapi import FastAPI

from app.local_live_guard import LocalLiveGuard


class LocalLiveGuardTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, *, peer="127.0.0.1", url="http://localhost/", headers=None, method="GET"):
        app = FastAPI()

        @app.get("/api/v1/capabilities")
        async def index():
            return {"ok": True}

        @app.get("/api/v1/collections/{collection_id}/keywords")
        async def collection_keywords(collection_id: str):
            return {"collection_id": collection_id}

        @app.get("/api/v1/collections/classifications/{keyword_id}")
        async def classification(keyword_id: str):
            return {"keyword_id": keyword_id}

        @app.get("/api/v1/collections/keywords/{keyword_id}")
        async def keyword_detail(keyword_id: str):
            return {"keyword_id": keyword_id}

        @app.post("/api/v1/collections/reconciliation/{run_id}/rollback")
        async def rollback(run_id: str):
            return {"run_id": run_id}

        @app.post("/api/v1/collections/{collection_id}/merge")
        async def merge(collection_id: str):
            return {"collection_id": collection_id}

        @app.post("/api/v1/collections/bulk")
        async def collection_bulk():
            return {"ok": True}

        @app.post("/api/v1/collections/reclassify")
        async def reclassify():
            return {"ok": True}

        @app.post("/api/v1/collections/reclassify-cached")
        async def reclassify_cached():
            return {"ok": True}

        transport = httpx.ASGITransport(app=LocalLiveGuard(app), client=(peer, 1234))
        async with httpx.AsyncClient(transport=transport, base_url=url) as client:
            return await client.request(method, url, headers=headers)

    async def test_loopback_and_same_origin_allowed(self):
        response = await self.request(
            url="http://localhost/api/v1/capabilities",
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(response.status_code, 200)

    async def test_hierarchical_topic_collection_reads_are_allowed(self):
        topic_id = "topic-a373d39f518501c4d5ddcc150a1e7d87"
        response = await self.request(url=f"http://localhost/api/v1/collections/{topic_id}/keywords")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["collection_id"], topic_id)

    async def test_bulk_collection_actions_are_allowed(self):
        response = await self.request(url="http://localhost/api/v1/collections/bulk", method="POST")
        self.assertEqual(response.status_code, 200)

    async def test_cached_reclassification_route_is_allowed(self):
        response = await self.request(url="http://localhost/api/v1/collections/reclassify", method="POST")
        self.assertEqual(response.status_code, 200)

    async def test_targeted_cached_reclassification_route_is_allowed(self):
        response = await self.request(url="http://localhost/api/v1/collections/reclassify-cached", method="POST")
        self.assertEqual(response.status_code, 200)

    async def test_classification_evidence_read_is_allowed(self):
        keyword_id = "a" * 64
        response = await self.request(url=f"http://localhost/api/v1/collections/classifications/{keyword_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["keyword_id"], keyword_id)

    async def test_persisted_keyword_workbench_detail_read_is_allowed(self):
        keyword_id = "b" * 64
        response = await self.request(url=f"http://localhost/api/v1/collections/keywords/{keyword_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["keyword_id"], keyword_id)

    async def test_local_reconciliation_rollback_is_allowed(self):
        run_id = "reconcile-12345678-1234-1234-1234-123456789abc"
        response = await self.request(url=f"http://localhost/api/v1/collections/reconciliation/{run_id}/rollback", method="POST")
        self.assertEqual(response.status_code, 200)

    async def test_collection_merge_is_allowed(self):
        collection_id = "12345678-1234-1234-1234-123456789abc"
        response = await self.request(
            url=f"http://localhost/api/v1/collections/{collection_id}/merge", method="POST"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["collection_id"], collection_id)

    async def test_remote_peer_host_proxy_and_cross_origin_rejected(self):
        for changes in ({"peer": "192.0.2.1"}, {"url": "http://evil.example/"},
                        {"headers": {"X-Forwarded-For": "127.0.0.1"}},
                        {"headers": {"Origin": "http://evil.example"}},
                        {"url": "https://localhost/"}):
            with self.subTest(changes=changes):
                response = await self.request(**changes)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["detail"]["code"], "LOCAL_ONLY")


if __name__ == "__main__":
    unittest.main()
