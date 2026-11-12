"""Public Fetch results feed source-scoped discovery without growth claims."""

import unittest
from datetime import datetime, timedelta, timezone

from app.discovery.contracts import ProcessingGrant
from app.discovery.source_results import discover_fetch_results
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
FETCHED = "2026-11-12T12:00:00+00:00"


def source_result(source_id, record, *, delivery="FRESH"):
    return {
        "source_id": source_id,
        "status": "SUCCESS",
        "data_mode": "SYNTHETIC",
        "delivery_state": delivery,
        "records": [record],
    }


def four_source_results():
    return [
        source_result(WIKIMEDIA_SOURCE_ID, {
            "article_display": "Quiet luxury", "article_original": "Quiet_luxury",
            "project": "en.wikipedia", "country_scope": "US", "access": "all-access",
            "observation_date": "2026-11-11", "fetched_at": FETCHED,
            "source_url": "https://en.wikipedia.org/wiki/Quiet_luxury",
        }),
        source_result(GOOGLE_SOURCE_ID, {
            "canonical_url": "https://example.org/google",
            "title": "Quiet luxury fashion guide", "snippet": "Quiet luxury outfit ideas",
            "fetched_at": FETCHED,
            "evidence": [{"query": {"canonical_query": "fashion trend",
                                     "query_fingerprint": "a" * 64}}],
        }, delivery="CACHED"),
        source_result(BING_SOURCE_ID, {
            "canonical_url": "https://example.org/bing", "title": "Quiet luxury styles",
            "snippet": "Fashion choices", "fetched_at": FETCHED,
            "evidence": [{"query": {"canonical_query": "fashion style",
                                     "query_fingerprint": "b" * 64}}],
        }),
        source_result(YOUTUBE_SOURCE_ID, {
            "provider_video_id": "AbCdEf12345", "canonical_video_url": "https://youtube.com/watch?v=AbCdEf12345",
            "title": "Quiet luxury wardrobe", "description": "Fashion overview",
            "fetched_at": FETCHED,
            "query_evidence": [{"canonical_query": "fashion wardrobe",
                                 "query_fingerprint": "c" * 64}],
        }),
    ]


class DistributedDiscoveryTests(unittest.TestCase):
    def test_public_fetch_records_become_source_scoped_keyword_evidence(self):
        result = discover_fetch_results(four_source_results(), now=NOW)
        candidate = next(item for item in result["candidates"] if item["keyword"] == "quiet luxury")

        self.assertEqual(result["data_mode"], "SYNTHETIC")
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(candidate["status"], "INSUFFICIENT_DATA")
        self.assertEqual(candidate["data_mode"], "SYNTHETIC")
        self.assertEqual({item["source_id"] for item in candidate["evidence"]}, {
            WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
        })
        self.assertIn("CACHED", {item["delivery_mode"] for item in candidate["evidence"]})
        self.assertEqual({item["classification"] for item in candidate["assessments"]},
                         {"INSUFFICIENT_EVIDENCE"})
        self.assertEqual({item["market_evidence"] for item in candidate["assessments"]}, {
            "US_COUNTRY_SCOPED", "US_FILTER_ONLY",
        })
        self.assertTrue(all(item["source_url"].startswith("https://")
                            for item in candidate["evidence"] if item["source_url"]))

    def test_invalid_record_is_reported_without_erasing_other_evidence(self):
        sources = four_source_results()
        sources[1]["records"][0]["canonical_url"] = "https://user:password@example.org/item"
        result = discover_fetch_results(sources, now=NOW)

        self.assertGreater(result["rejected_record_count"], 0)
        candidate = next(item for item in result["candidates"] if item["keyword"] == "quiet luxury")
        self.assertNotIn(GOOGLE_SOURCE_ID, {item["source_id"] for item in candidate["evidence"]})
        self.assertEqual(candidate["status"], "INSUFFICIENT_DATA")

    def test_query_mode_is_not_relabelled_as_live_and_results_are_bounded(self):
        source = source_result(GOOGLE_SOURCE_ID, {
            "canonical_url": "https://example.org/item", "title": "linen pants",
            "fetched_at": FETCHED,
        })
        source["data_mode"] = "LIVE"
        result = discover_fetch_results([source], now=NOW)

        self.assertEqual(result["data_mode"], "SYNTHETIC")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["rejected_record_count"], 1)

    def test_live_without_extraction_grant_reports_processing_blocked(self):
        source = source_result(WIKIMEDIA_SOURCE_ID, {
            "article_display": "Quiet luxury", "article_original": "Quiet_luxury",
            "project": "en.wikipedia", "country_scope": "US", "access": "all-access",
            "observation_date": "2026-11-10", "fetched_at": FETCHED,
            "source_url": "https://en.wikipedia.org/wiki/Quiet_luxury",
        })
        source["data_mode"] = "LIVE"

        result = discover_fetch_results([source], now=NOW)

        self.assertEqual(result["status"], "DISCOVERY_PROCESSING_BLOCKED")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["rejected_record_count"], 1)
        self.assertIn("DISCOVERY_PROCESSING_BLOCKED", result["limitations"])

    def test_empty_live_source_without_grant_still_explains_processing_block(self):
        result = discover_fetch_results([{
            "source_id": WIKIMEDIA_SOURCE_ID,
            "status": "FAILED",
            "data_mode": "LIVE",
            "records": [],
        }], now=NOW)

        self.assertEqual(result["status"], "DISCOVERY_PROCESSING_BLOCKED")
        self.assertEqual(result["candidate_count"], 0)
        self.assertIn("DISCOVERY_PROCESSING_BLOCKED", result["limitations"])

    def test_live_requires_source_grant_and_preserves_mixed_provenance(self):
        sources = four_source_results()
        sources[1]["data_mode"] = "LIVE"
        sources[3]["data_mode"] = "LIVE"
        grant = ProcessingGrant(GOOGLE_SOURCE_ID, True, False,
                                NOW + timedelta(minutes=5), "test-policy")
        result = discover_fetch_results(sources, now=NOW, processing_grants=(grant,))
        candidate = next(item for item in result["candidates"] if item["keyword"] == "quiet luxury")
        self.assertEqual(result["data_mode"], "MIXED")
        self.assertEqual(candidate["data_mode"], "MIXED")
        self.assertEqual({item["data_mode"] for item in candidate["evidence"]}, {"LIVE", "SYNTHETIC"})
        self.assertIn("LANGUAGE_UNVERIFIED", candidate["quality_flags"])
        self.assertTrue(all(item["language"] == "und" for item in candidate["evidence"]
                            if item["data_mode"] == "LIVE"))
        self.assertNotIn(YOUTUBE_SOURCE_ID, {item["source_id"] for item in candidate["evidence"]})
        self.assertEqual(result["rejected_record_count"], 1)
        self.assertEqual(candidate["status"], "INSUFFICIENT_DATA")

    def test_expired_live_grant_is_rejected(self):
        source = four_source_results()[1]
        source["data_mode"] = "LIVE"
        grant = ProcessingGrant(GOOGLE_SOURCE_ID, True, False,
                                NOW - timedelta(seconds=1), "test-policy")
        result = discover_fetch_results([source], now=NOW, processing_grants=(grant,))
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["rejected_record_count"], 1)

    def test_synthetic_record_cannot_be_relabelled_live(self):
        source = four_source_results()[1]
        source["data_mode"] = "LIVE"
        source["records"][0]["data_mode"] = "SYNTHETIC"
        grant = ProcessingGrant(GOOGLE_SOURCE_ID, True, False,
                                NOW + timedelta(minutes=5), "test-policy")
        result = discover_fetch_results([source], now=NOW, processing_grants=(grant,))
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["rejected_record_count"], 1)


if __name__ == "__main__":
    unittest.main()
