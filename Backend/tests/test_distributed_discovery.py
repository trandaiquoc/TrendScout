"""Public Fetch results feed source-scoped discovery without growth claims."""

import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from app.analysis.keyword_identity import canonical_keyword_digest
from app.discovery.contracts import ProcessingGrant
from app.discovery.source_results import discover_fetch_results
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.google_trends.trending_now import normalize_keyword

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
            "data_source": "GOOGLE_TRENDS", "canonical_keyword": "QUIET luxury",
            "source_keyword_raw": "QUIET luxury",
            "canonical_keyword_identity": canonical_keyword_digest("QUIET luxury"),
            "provider_id": "serpapi_google_trends_trending_now", "transport_id": "serpapi",
            "discovery_mode": "SERPAPI_TRENDING_NOW", "region": "US", "language": "en",
            "time_window_hours": 24,
            "trend_clusters": [{
                "trend_cluster_id": "trend-1", "started_at": "2026-11-12T11:00:00+00:00",
                "search_volume": None, "search_volume_display": None,
                "increase_percentage": None,
            }],
            "source_url": "https://trends.google.com/trending?geo=US&hl=en-US",
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
        candidate = next(item for item in result["candidates"]
                         if item["keyword"].casefold() == "quiet luxury")

        self.assertEqual(result["data_mode"], "SYNTHETIC")
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(candidate["status"], "INSUFFICIENT_DATA")
        self.assertEqual(candidate["data_mode"], "SYNTHETIC")
        self.assertTrue(candidate["taxonomy_version"])
        self.assertTrue(candidate["extraction_version"])
        self.assertTrue(candidate["classification_version"])
        self.assertEqual(candidate["canonical_keyword_identity"], candidate["keyword_id"])
        self.assertEqual({item["source_id"] for item in candidate["evidence"]}, {
            WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID,
        })
        self.assertIn("CACHED", {item["delivery_mode"] for item in candidate["evidence"]})
        self.assertEqual({item["classification"] for item in candidate["assessments"]},
                         {"INSUFFICIENT_EVIDENCE"})
        self.assertEqual({item["market_evidence"] for item in candidate["assessments"]}, {
            "US_COUNTRY_SCOPED", "US_FILTER_ONLY",
        })
        self.assertTrue(all(item["source_url"].startswith("https://")
                            for item in candidate["evidence"] if item["source_url"]))
        self.assertEqual({item["canonical_keyword_identity"] for item in candidate["evidence"]},
                         {candidate["keyword_id"]})
        self.assertEqual({item["raw_source_keyword"] for item in candidate["evidence"]},
                         {"Quiet_luxury", "QUIET luxury"})
        google_evidence = next(item for item in candidate["evidence"]
                               if item["source_id"] == GOOGLE_SOURCE_ID)
        self.assertEqual(google_evidence["observed_at"], "2026-11-12T11:00:00+00:00")
        self.assertIsNone(google_evidence["source_metrics"]["trend_clusters"][0]["search_volume"])
        self.assertEqual(google_evidence["source_metric_unit"], "provider-defined per cluster")
        self.assertEqual(result["rejected_record_count"], 2)
        self.assertIn("YOUTUBE_KEYWORD_EXTRACTION_DISABLED", result["limitations"])
        self.assertEqual({item["parser_version"] for item in candidate["evidence"]}, {"source-v1"})
        self.assertEqual({item["algorithm_version"] for item in candidate["assessments"]},
                         {"sample-presence-v1"})
        self.assertEqual(result, discover_fetch_results(four_source_results(), now=NOW))

    def test_fetch_display_order_uses_identity_not_business_category_priority(self):
        records = []
        for phrase in ("linen dress", "portable solar generator"):
            _, identity = normalize_keyword(phrase)
            records.append({
                "data_source": "GOOGLE_TRENDS",
                "canonical_keyword": phrase,
                "canonical_keyword_identity": hashlib.sha256(
                    identity.encode("utf-8")
                ).hexdigest(),
                "provider_id": "serpapi_google_trends_trending_now",
                "transport_id": "serpapi",
                "discovery_mode": "SERPAPI_TRENDING_NOW",
                "region": "US", "language": "en", "time_window_hours": 24,
                "fetched_at": FETCHED,
                "source_url": "https://trends.google.com/trending?geo=US&hl=en-US",
                "trend_clusters": [{"trend_cluster_id": phrase,
                                    "increase_percentage": 50}],
            })
        result = discover_fetch_results([{
            "source_id": GOOGLE_SOURCE_ID, "status": "SUCCESS",
            "data_mode": "SYNTHETIC", "records": records,
        }], now=NOW)
        candidates = result["candidates"]
        self.assertEqual([item["keyword_id"] for item in candidates],
                         sorted(item["keyword_id"] for item in candidates))
        self.assertEqual({item["keyword"] for item in candidates},
                         {"linen dress", "portable solar generator"})

    def test_global_wikimedia_scope_stays_global_beside_us_filtered_trends(self):
        sources = four_source_results()
        sources[0]["records"][0]["country_scope"] = "GLOBAL"
        result = discover_fetch_results(sources, now=NOW)

        candidate = next(item for item in result["candidates"]
                         if item["keyword"].casefold() == "quiet luxury")
        geography = {item["source_id"]: item["market_evidence"]
                     for item in candidate["evidence"]}
        self.assertEqual(geography[WIKIMEDIA_SOURCE_ID], "GLOBAL")
        self.assertEqual(geography[GOOGLE_SOURCE_ID], "US_FILTER_ONLY")
        assessments = {item["source_id"]: item["market_evidence"]
                       for item in candidate["assessments"]}
        self.assertEqual(assessments[WIKIMEDIA_SOURCE_ID], "GLOBAL")
        self.assertEqual(assessments[GOOGLE_SOURCE_ID], "US_FILTER_ONLY")

    def test_invalid_record_is_reported_without_erasing_other_evidence(self):
        sources = four_source_results()
        sources[1]["records"][0]["source_url"] = "https://user:password@example.org/item"
        result = discover_fetch_results(sources, now=NOW)

        self.assertGreater(result["rejected_record_count"], 0)
        candidate = next(item for item in result["candidates"]
                         if item["keyword"].casefold() == "quiet luxury")
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
        youtube_grant = ProcessingGrant(YOUTUBE_SOURCE_ID, True, True,
                                        NOW + timedelta(minutes=5), "youtube-test-grant")
        result = discover_fetch_results(
            sources, now=NOW, processing_grants=(grant, youtube_grant)
        )
        candidate = next(item for item in result["candidates"]
                         if item["keyword"].casefold() == "quiet luxury")
        self.assertEqual(result["data_mode"], "MIXED")
        self.assertEqual(candidate["data_mode"], "MIXED")
        self.assertEqual({item["data_mode"] for item in candidate["evidence"]}, {"LIVE", "SYNTHETIC"})
        self.assertIn("LANGUAGE_UNVERIFIED", candidate["quality_flags"])
        self.assertTrue(all(item["language"] == "und" for item in candidate["evidence"]
                            if item["data_mode"] == "LIVE"))
        self.assertNotIn(YOUTUBE_SOURCE_ID, {item["source_id"] for item in candidate["evidence"]})
        self.assertEqual(result["rejected_record_count"], 2)
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
