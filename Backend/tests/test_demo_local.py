"""The presentation app uses fixtures and ephemeral storage without credentials."""

import asyncio
import unittest
from unittest.mock import patch

import httpx

from app.demo_local import create_demo_app


class LocalDemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_anonymous_fetch_is_synthetic_without_cloud_configuration(self):
        with patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT": "personal-project"}):
            app = create_demo_app()
        self.assertIsNone(app.state.settings.google_cloud_project)
        self.assertIsNone(app.state.settings.operator_token)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1") as client:
                capability = (await client.get("/api/v1/capabilities")).json()
                self.assertTrue(capability["public_fetch_enabled"])
                self.assertEqual(capability["data_mode"], "SYNTHETIC")
                # No token is provisioned in demo; the legacy route fails closed.
                self.assertEqual((await client.post("/api/runs")).status_code, 503)
                accepted = await client.post("/api/v1/fetches", json={}, headers={
                    "Origin": "http://127.0.0.1", "Idempotency-Key": "presentation-test-0001"})
                self.assertEqual(accepted.status_code, 202)
                for _ in range(200):
                    result = (await client.get(accepted.json()["status_url"])).json()
                    if result["terminal"]:
                        break
                    await asyncio.sleep(0.005)
                self.assertEqual(result["data_mode"], "SYNTHETIC")
                self.assertIn(result["status"], {"COMPLETED", "PARTIAL"})
                run = (await client.get("/api/runs/" + result["run_id"])).json()
                self.assertEqual(run["mode"], "synthetic")
                keywords = (await client.get("/api/runs/" + result["run_id"] + "/keywords")).json()
                self.assertGreater(keywords["total"], 0)


if __name__ == "__main__":
    unittest.main()
