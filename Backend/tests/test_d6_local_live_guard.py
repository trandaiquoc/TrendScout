import unittest

import httpx
from fastapi import FastAPI

from app.local_live_guard import LocalLiveGuard


class LocalLiveGuardTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, *, peer="127.0.0.1", url="http://localhost/", headers=None):
        app = FastAPI()

        @app.get("/api/v1/capabilities")
        async def index():
            return {"ok": True}

        transport = httpx.ASGITransport(app=LocalLiveGuard(app), client=(peer, 1234))
        async with httpx.AsyncClient(transport=transport, base_url=url) as client:
            return await client.get(url, headers=headers)

    async def test_loopback_and_same_origin_allowed(self):
        response = await self.request(
            url="http://localhost/api/v1/capabilities",
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(response.status_code, 200)

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
