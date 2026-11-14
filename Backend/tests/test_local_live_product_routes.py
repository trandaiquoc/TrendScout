"""The guarded local workspace exposes only its declared product routes."""

import unittest

import httpx
from fastapi import FastAPI

from app.local_live_guard import LocalLiveGuard


class LocalLiveProductRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_reads_and_bounded_writes_pass_the_loopback_route_gate(self):
        app = FastAPI()

        @app.get("/api/v1/runtime")
        @app.get("/api/v1/analytics/overview")
        @app.get("/api/v1/sources/library")
        @app.get("/api/v1/research")
        @app.get("/api/v1/scheduler")
        @app.get("/api/v1/runs/history")
        @app.get("/api/v1/runs/history/{item_id}")
        async def read_route(item_id=None):
            return {"item_id": item_id}

        @app.post("/api/v1/research")
        @app.patch("/api/v1/research/{item_id}")
        @app.post("/api/v1/scheduler")
        @app.patch("/api/v1/scheduler/{item_id}")
        async def write_route(item_id=None):
            return {"item_id": item_id}

        guarded = LocalLiveGuard(app)
        transport = httpx.ASGITransport(app=guarded, client=("127.0.0.1", 12000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            for path in (
                "/api/v1/runtime", "/api/v1/analytics/overview", "/api/v1/sources/library",
                "/api/v1/research", "/api/v1/scheduler", "/api/v1/runs/history",
                "/api/v1/runs/history/" + "a" * 32,
                "/api/v1/runs/history/context-classification-preview",
                "/api/v1/runs/history/organize-7b406cfd-e1c5-46b3-b592-53e5cbb9eaf4",
            ):
                with self.subTest(method="GET", path=path):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 200, response.text)

            origin = {"Origin": "http://127.0.0.1"}
            for method, path in (
                ("POST", "/api/v1/research"),
                ("PATCH", "/api/v1/research/" + "b" * 32),
                ("POST", "/api/v1/scheduler"),
                ("PATCH", "/api/v1/scheduler/" + "c" * 32),
            ):
                with self.subTest(method=method, path=path):
                    response = await client.request(method, path, json={}, headers=origin)
                    self.assertEqual(response.status_code, 200, response.text)

            denied = await client.get("/api/v1/internal-admin")
            self.assertEqual(denied.status_code, 404)
            denied_run_id = await client.get("/api/v1/runs/history/unsafe.id")
            self.assertEqual(denied_run_id.status_code, 404)


if __name__ == "__main__":
    unittest.main()
