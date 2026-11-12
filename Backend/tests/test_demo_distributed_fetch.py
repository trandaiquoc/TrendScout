"""The local presentation demo serves one-click, four-source synthetic Fetch."""

import tempfile
import unittest
from pathlib import Path

import httpx

from app.demo_local import create_demo_app


class DemoDistributedFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_fetch_uses_local_synthetic_fixtures_end_to_end(self):
        application = create_demo_app()
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://127.0.0.1",
            ) as client:
                capability = await client.get("/api/v1/capabilities")
                accepted = await client.post(
                    "/api/v1/fetches", json={}, headers={
                        "Origin": "http://127.0.0.1",
                        "Idempotency-Key": "demo-public-fetch-offline-0001",
                    },
                )
                self.assertEqual(accepted.status_code, 202, accepted.text)
                status = await client.get(accepted.json()["status_url"])

        self.assertEqual(capability.status_code, 200)
        self.assertTrue(capability.json()["public_fetch_enabled"])
        self.assertEqual(capability.json()["data_mode"], "SYNTHETIC")
        self.assertEqual(status.status_code, 200, status.text)
        value = status.json()
        self.assertTrue(value["terminal"])
        self.assertEqual(value["data_mode"], "SYNTHETIC")
        self.assertEqual({source["source_id"] for source in value["sources"]}, {
            "wikimedia_pageviews_top_per_country", "google_web_serp", "bing_web_serp",
            "youtube_video_search",
        })
        self.assertTrue(all(source["data_mode"] == "SYNTHETIC"
                            for source in value["sources"]))
        self.assertTrue(all(source["status"] == "SUCCESS" for source in value["sources"]))
        discovery = value["discovery"]
        self.assertEqual(discovery["status"], "INSUFFICIENT_EVIDENCE")
        candidate = next(item for item in discovery["candidates"]
                         if item["keyword"] == "quiet luxury")
        self.assertEqual(candidate["data_mode"], "SYNTHETIC")
        self.assertEqual(candidate["status"], "INSUFFICIENT_DATA")
        self.assertEqual({item["source_id"] for item in candidate["evidence"]}, {
            "wikimedia_pageviews_top_per_country", "google_web_serp", "bing_web_serp",
            "youtube_video_search",
        })
        self.assertTrue(all(item["source_url"] is None for item in candidate["evidence"]))
        self.assertFalse(hasattr(application.state.settings, "api_key"))
        self.assertTrue(Path(application.state.demo_directory.name).is_dir())
        self.assertTrue(application.state.demo_directory.name.startswith(tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main()
