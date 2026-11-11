"""The presentation app uses fixtures and ephemeral storage without credentials."""

import asyncio
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import httpx

from app.demo_local import create_demo_app
from app.main import create_app
from app.models.observations import CollectionResult, DatasetSnapshot, FetchStatus
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.synthetic_multisource import collect_synthetic_discovery


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
                self.assertEqual(sum(x["enabled_for_current_operation"]
                                     for x in capability["sources"]), 4)
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
                self.assertEqual(result["status"], "PARTIAL")
                self.assertEqual(len(result["sources"]), 4)
                run = (await client.get("/api/runs/" + result["run_id"])).json()
                self.assertEqual(run["mode"], "synthetic")
                supplement = run["synthetic_discovery"]
                self.assertEqual(supplement["data_mode"], "SYNTHETIC")
                self.assertEqual({s["source_id"] for s in supplement["sources"]},
                                 {GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID})
                self.assertTrue(all(s["status"] == "PARTIAL" and not s["complete"]
                                    for s in supplement["sources"]))
                self.assertTrue(supplement["candidates"])
                for candidate in supplement["candidates"]:
                    self.assertEqual(candidate["data_mode"], "SYNTHETIC")
                    self.assertNotIn("synthetic", candidate["keyword"])
                    self.assertTrue(all(a["classification"] == "INSUFFICIENT_EVIDENCE"
                                        for a in candidate["assessments"]))
                    self.assertTrue(all(e["data_mode"] == "SYNTHETIC"
                                        for e in candidate["evidence"]))
                self.assertNotIn("offline-fixture-placeholder", str(run))
                keywords = (await client.get("/api/runs/" + result["run_id"] + "/keywords")).json()
                self.assertGreater(keywords["total"], 0)

    async def test_synthetic_failure_retains_wikimedia_results(self):
        async def fail():
            raise RuntimeError("private provider detail should not leak")
        fail.__trendscout_offline_only__ = True
        app = create_demo_app()
        async with app.router.lifespan_context(app):
            app.state.run_service.synthetic_supplement = fail
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1") as client:
                accepted = await client.post("/api/v1/fetches", json={}, headers={
                    "Origin": "http://127.0.0.1", "Idempotency-Key": "fixture-failure-test-1"})
                for _ in range(200):
                    result = (await client.get(accepted.json()["status_url"])).json()
                    if result["terminal"]:
                        break
                    await asyncio.sleep(0.005)
                self.assertEqual(result["status"], "PARTIAL")
                run = (await client.get("/api/runs/" + result["run_id"])).json()
                self.assertEqual(run["synthetic_discovery"]["error"], "SYNTHETIC_SUPPLEMENT_FAILED")
                self.assertNotIn("private provider detail", str(run))
                self.assertGreater(run["keyword_count"], 0)

    async def test_fixture_bundle_is_deterministic(self):
        self.assertEqual(await collect_synthetic_discovery(), await collect_synthetic_discovery())

    async def test_wikimedia_failure_preserves_query_source_results(self):
        class UnavailableWikimedia:
            async def collect(self):
                snapshot = DatasetSnapshot(
                    source="wikimedia_pageviews_top_per_country", country_scope="US",
                    access="all-access", observation_date=date(2026, 1, 8),
                    fetched_at=datetime(2026, 1, 9, tzinfo=timezone.utc),
                    fetch_status=FetchStatus.FETCH_ERROR, http_status=None,
                    item_count=None, request_url="synthetic://unavailable",
                )
                return CollectionResult(None, ((snapshot, []),))
        app = create_demo_app()
        async with app.router.lifespan_context(app):
            app.state.run_service.adapter_factory = UnavailableWikimedia
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1") as client:
                accepted = await client.post("/api/v1/fetches", json={}, headers={
                    "Origin": "http://127.0.0.1", "Idempotency-Key": "wiki-failure-test-001"})
                for _ in range(200):
                    result = (await client.get(accepted.json()["status_url"])).json()
                    if result["terminal"]:
                        break
                    await asyncio.sleep(0.005)
                self.assertEqual(result["status"], "PARTIAL")
                run = (await client.get("/api/runs/" + result["run_id"])).json()
                self.assertTrue(run["synthetic_discovery"]["candidates"])
                self.assertEqual(run["coverage"][0]["status"], "FETCH_ERROR")
                keywords = await client.get("/api/runs/" + result["run_id"] + "/keywords")
                self.assertEqual(keywords.json()["total"], 0)

    async def test_failed_provider_does_not_hide_other_fixture_sources(self):
        def one_failure(request):
            if request.url.params.get("engine") == "google":
                return httpx.Response(429, json={"error": "synthetic quota exhausted"})
            return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                "organic_results": [{"position": 1, "link": "https://example.org/sample",
                                     "title": "Synthetic kitchen organizer"}]})
        with patch("app.synthetic_multisource._search_response", one_failure):
            bundle = await collect_synthetic_discovery()
        statuses = {source["source_id"]: source for source in bundle["sources"]}
        self.assertEqual(statuses[GOOGLE_SOURCE_ID]["status"], "FAILED")
        self.assertEqual(statuses[BING_SOURCE_ID]["record_count"], 1)
        self.assertEqual(statuses[YOUTUBE_SOURCE_ID]["record_count"], 1)

    async def test_supplement_cannot_be_wired_into_default_live_runtime(self):
        with self.assertRaises(ValueError):
            create_app(synthetic_supplement=collect_synthetic_discovery)


if __name__ == "__main__":
    unittest.main()
