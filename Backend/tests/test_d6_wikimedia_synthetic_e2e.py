"""Wikimedia-shaped API/worker/discovery path, isolated and offline-only."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.discovery.source_results import discover_fetch_results
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.wikimedia.adapter import _parse
from d6_wikimedia_synthetic_support import (
    FETCHED_AT,
    OBSERVATION_DATE,
    RAW_BODY_MARKER,
    SOURCE_IDS,
    build_synthetic_app,
    fixture_payload,
    synthetic_settings,
)


class WikimediaSyntheticEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="d6-2-3-wikimedia-")
        self.root = Path(self.directory.name)
        self.app = build_synthetic_app(str(self.root))
        self.assertEqual(self.app.state.d6_2_3_state_store.data_mode, "SYNTHETIC")
        self.assertTrue(self.app.state.d6_2_3_state_store.path.is_relative_to(self.root))
        self.assertTrue(self.app.state.d6_2_3_wikimedia_gateway._offline)
        self.assertIsInstance(self.app.state.d6_2_3_wikimedia_gateway._inner,
                              httpx.MockTransport)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        await self.app.state.d6_2_3_wikimedia_gateway.aclose()
        self.directory.cleanup()

    def headers(self, key="d6-2-3-wikimedia-fetch-0001"):
        return {"Origin": "http://127.0.0.1", "Idempotency-Key": key}

    async def test_actual_fetch_worker_checkpoint_discovery_and_replay(self):
        self.assertEqual(self.app.state.d6_2_3_mock_request_counts[WIKIMEDIA_SOURCE_ID], 0,
                         "application startup must not request the fixture")

        capabilities = await self.client.get("/api/v1/capabilities")
        self.assertEqual(capabilities.status_code, 200)
        capability_body = capabilities.json()
        self.assertTrue(capability_body["fetch_enabled"])
        self.assertEqual(capability_body["data_mode"], "SYNTHETIC")
        self.assertEqual({row["source_id"] for row in capability_body["sources"]}, SOURCE_IDS)
        self.assertNotIn("YOUTUBE_API_KEY", json.dumps(capability_body))
        self.assertNotIn("SERPAPI_API_KEY", json.dumps(capability_body))
        self.assertEqual(self.app.state.d6_2_3_mock_request_counts[WIKIMEDIA_SOURCE_ID], 0)

        accepted = await self.client.post(
            "/api/v1/fetches", json={}, headers=self.headers()
        )
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(accepted.json()["data_mode"], "SYNTHETIC")
        self.assertEqual(accepted.headers["idempotent-replay"], "false")
        fetch_id = accepted.json()["fetch_id"]

        first = await self.client.get(f"/api/v1/fetches/{fetch_id}",
                                      headers={"Origin": "http://127.0.0.1"})
        self.assertEqual(first.status_code, 200)
        result = first.json()
        self.assertTrue(result["terminal"])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["data_mode"], "SYNTHETIC")
        self.assertEqual(self.app.state.d6_2_3_mock_request_counts[WIKIMEDIA_SOURCE_ID], 1)

        sources = {row["source_id"]: row for row in result["sources"]}
        self.assertEqual(set(sources), SOURCE_IDS)
        wiki = sources[WIKIMEDIA_SOURCE_ID]
        self.assertEqual(wiki["status"], "PARTIAL")
        self.assertEqual(wiki["data_mode"], "SYNTHETIC")
        self.assertEqual(wiki["record_count"], 10)
        self.assertEqual(wiki["coverage"], {
            "observation_date": "2026-11-10", "rejected": 4,
            "records_omitted_storage_limit": 0, "item_count": 14,
        })
        records = wiki["records"]
        by_article = {record["article_original"]: record for record in records}
        linen_rows = [record for record in records
                      if record["article_original"] == "Linen_Pants"]
        self.assertIn("Linen_Pants", by_article)
        self.assertEqual(linen_rows[0]["provider_rank"], 1)
        self.assertEqual(linen_rows[0]["views_ceil"], 512)
        self.assertEqual(linen_rows[0]["source"], WIKIMEDIA_SOURCE_ID)
        self.assertEqual(by_article["Linen_Pants"]["country_scope"], "US")
        self.assertEqual(by_article["Linen_Pants"]["access"], "all-access")
        self.assertEqual(by_article["Linen_Pants"]["project"], "en.wikipedia")
        self.assertEqual(by_article["Linen_Pants"]["observation_date"], "2026-11-10")
        self.assertEqual(by_article["Linen_Pants"]["fetched_at"], FETCHED_AT.isoformat())
        self.assertEqual(by_article["Linen_Pants"]["source_url"],
                         "https://en.wikipedia.org/wiki/Linen_Pants")
        self.assertEqual(by_article["Linen_Pants"]["quality_flags"],
                         ["TOP_LIST_ONLY", "VIEWS_APPROXIMATE"])
        self.assertEqual(len(linen_rows), 2,
                         "the current parser preserves duplicate rows and does not reject them")
        self.assertIn("<img src=x onerror=alert(1)>", by_article)

        for source_id in (GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID):
            self.assertEqual(sources[source_id]["status"], "UNAVAILABLE")
            self.assertEqual(sources[source_id]["record_count"], 0)
            self.assertEqual(sources[source_id]["records"], [])

        discovery = result["discovery"]
        self.assertEqual(discovery["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(discovery["data_mode"], "SYNTHETIC")
        self.assertTrue({"SYNTHETIC_ONLY", "NOT_LIVE_VERIFIED", "NO_BASELINE",
                         "COVERAGE_LIMITED"}.issubset(discovery["limitations"]))
        candidates = discovery["candidates"]
        self.assertEqual(discovery["candidate_count"], 36)
        self.assertEqual(discovery["shown_count"], 36)
        self.assertTrue(all(candidate["score"] is None for candidate in candidates))
        self.assertTrue(all(candidate["data_mode"] == "SYNTHETIC" for candidate in candidates))
        self.assertTrue(all(candidate["taxonomy_version"] and candidate["extraction_version"]
                            and candidate["classification_version"] for candidate in candidates))
        self.assertTrue(all(
            assessment["classification"] == "INSUFFICIENT_EVIDENCE"
            and "Comparable complete sample coverage and baseline are required."
                in assessment["reason"]
            and assessment["algorithm_version"] == "sample-presence-v1"
            for candidate in candidates for assessment in candidate["assessments"]
        ))
        candidate_by_text = {candidate["keyword"]: candidate for candidate in candidates}
        self.assertIn("linen pants", candidate_by_text)
        self.assertIn("fashion", candidate_by_text["linen pants"]["categories"])
        self.assertIn("RULE_BASED_UNVALIDATED", candidate_by_text["linen pants"]["quality_flags"])
        self.assertIn("COVERAGE_LIMITED", candidate_by_text["linen pants"]["quality_flags"])
        self.assertIn("kitchen organizer", candidate_by_text)
        self.assertIn("home_living", candidate_by_text["kitchen organizer"]["categories"])
        self.assertIn("robotics", candidate_by_text)
        self.assertIn("technology", candidate_by_text["robotics"]["categories"])
        linen_evidence = candidate_by_text["linen pants"]["evidence"][0]
        self.assertEqual(linen_evidence["source_id"], WIKIMEDIA_SOURCE_ID)
        self.assertEqual(linen_evidence["provider_id"], "wikimedia_aqs")
        self.assertEqual(linen_evidence["observed_at"][:10], "2026-11-10")
        self.assertEqual(linen_evidence["fetched_at"], FETCHED_AT.isoformat())
        self.assertEqual(linen_evidence["market_evidence"], "US_COUNTRY_SCOPED")
        self.assertEqual(linen_evidence["data_mode"], "SYNTHETIC")
        self.assertEqual(linen_evidence["delivery_mode"], "FRESH")
        self.assertEqual(linen_evidence["parser_version"], "source-v1")
        self.assertEqual(linen_evidence["scope"],
                         "wikimedia:US:all-access:en.wikipedia")
        self.assertNotIn("views_ceil", linen_evidence)
        self.assertNotIn("search_volume", json.dumps(result).lower())
        self.assertNotIn(RAW_BODY_MARKER, json.dumps(result))
        self.assertFalse(any("LIVE_VERIFIED" == candidate["data_mode"]
                             for candidate in candidates))

        job = self.app.state.distributed_public_fetch.admission.read_job(fetch_id, now=FETCHED_AT)
        checkpoint = job["source_checkpoints"][WIKIMEDIA_SOURCE_ID]["snapshot"]
        checkpoint_entry = job["source_checkpoints"][WIKIMEDIA_SOURCE_ID]
        self.assertEqual(checkpoint["data_mode"], "SYNTHETIC")
        self.assertEqual(checkpoint["phase"], "COMPLETE")
        self.assertEqual(checkpoint["result"]["record_count"], 10)
        self.assertEqual(checkpoint_entry["policy_version"], "d6-2-3-checkpoint-v1")
        self.assertLessEqual(
            datetime.fromisoformat(checkpoint_entry["retain_until"]),
            FETCHED_AT + timedelta(seconds=300),
        )
        self.assertNotIn(RAW_BODY_MARKER, json.dumps(checkpoint))
        self.assertEqual(await self.app.state.distributed_public_fetch.run_once("recovery-worker"), None)
        self.assertEqual(self.app.state.d6_2_3_mock_request_counts[WIKIMEDIA_SOURCE_ID], 1)

        second = await self.client.get(f"/api/v1/fetches/{fetch_id}",
                                       headers={"Origin": "http://127.0.0.1"})
        self.assertEqual(second.json(), result)
        replay = await self.client.post(
            "/api/v1/fetches", json={}, headers=self.headers()
        )
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["fetch_id"], fetch_id)
        self.assertEqual(replay.headers["idempotent-replay"], "true")
        self.assertEqual(self.app.state.d6_2_3_mock_request_counts[WIKIMEDIA_SOURCE_ID], 1)

        connection = sqlite3.connect(
            f"file:{self.app.state.d6_2_3_state_store.path.as_posix()}?mode=ro", uri=True
        )
        try:
            rows = connection.execute("SELECT state_json FROM atomic_state").fetchall()
        finally:
            connection.close()
        self.assertTrue(rows)
        self.assertNotIn(RAW_BODY_MARKER, "".join(row[0] for row in rows))

    def test_existing_parser_rejects_wrong_country_access_and_date(self):
        payload = fixture_payload()
        day = OBSERVATION_DATE
        fetched = FETCHED_AT
        for field, value in (("country", "CA"), ("access", "desktop"), ("day", "09")):
            malformed = json.loads(json.dumps(payload))
            malformed["items"][0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    _parse(malformed, day, fetched)

    def test_live_discovery_without_extraction_grant_is_blocked(self):
        live = {
            "source_id": WIKIMEDIA_SOURCE_ID,
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "records": [{
                "article_original": "Linen_Pants",
                "article_display": "Linen Pants",
                "project": "en.wikipedia",
                "country_scope": "US",
                "access": "all-access",
                "observation_date": OBSERVATION_DATE.isoformat(),
                "fetched_at": FETCHED_AT.isoformat(),
                "source_url": "https://en.wikipedia.org/wiki/Linen_Pants",
            }],
        }
        result = discover_fetch_results([live], now=FETCHED_AT, processing_grants=())
        self.assertEqual(result["status"], "DISCOVERY_PROCESSING_BLOCKED")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["candidates"], [])

    def test_synthetic_handlers_cannot_be_relabelled_live(self):
        modes = set(self.app.state.distributed_public_fetch.worker.source_modes.values())
        self.assertEqual(modes, {"SYNTHETIC"})
        self.assertIsNone(self.app.state.distributed_public_fetch.resolve_processing_grants)


if __name__ == "__main__":
    unittest.main()
