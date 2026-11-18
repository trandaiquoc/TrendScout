"""Security gates for the temporary Production Firestore smoke route."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app.api.firestore_smoke import router


class FirestoreSmokeRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.state.read_only = True
        self.app.state.operator_token = "operator-token-that-is-at-least-32-bytes-long"
        self.app.state.read_storage_factory = lambda _token: self.fail(
            "storage factory must not run before the auth gates"
        )
        self.app.include_router(router, prefix="/api/internal")
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        self.environment = patch.dict(os.environ, {
            "VERCEL": "1",
            "VERCEL_ENV": "production",
            "TREND_SCOUT_FIRESTORE_SMOKE_ENABLED": "1",
            "TREND_SCOUT_FIRESTORE_SMOKE_EXPIRES_AT": expires,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def _post(self, *, authorization=None):
        headers = {"x-vercel-oidc-token": "unused-by-auth-gate-test"}
        if authorization is not None:
            headers["authorization"] = authorization
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://testserver"
        ) as client:
            return await client.post(
                "/api/internal/firestore-smoke/write",
                headers=headers,
                json={"run_id": "a" * 32},
            )

    async def test_wrong_operator_token_is_rejected_before_storage_creation(self):
        response = await self._post(authorization="Bearer wrong")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Operator authentication required"})

    async def test_expired_enable_window_hides_the_route_before_storage_creation(self):
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with patch.dict(os.environ, {"TREND_SCOUT_FIRESTORE_SMOKE_EXPIRES_AT": expired}):
            response = await self._post(
                authorization=f"Bearer {self.app.state.operator_token}"
            )

        self.assertEqual(response.status_code, 404)

    async def test_non_production_environment_hides_the_route(self):
        with patch.dict(os.environ, {"VERCEL_ENV": "preview"}):
            response = await self._post(
                authorization=f"Bearer {self.app.state.operator_token}"
            )

        self.assertEqual(response.status_code, 404)

    async def test_request_rejects_extra_fields(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://testserver"
        ) as client:
            response = await client.post(
                "/api/internal/firestore-smoke/write",
                headers={"authorization": f"Bearer {self.app.state.operator_token}"},
                json={"run_id": "a" * 32, "project": "other"},
            )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
