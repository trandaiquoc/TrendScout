"""Offline discovery contracts; no provider or cloud connection is used."""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.analysis.keyword_identity import canonical_keyword_digest, normalize_keyword_text
from app.discovery.assessment import assess_candidate
from app.discovery.contracts import CoverageDay, Observation, ProcessingGrant
from app.discovery.extraction import extract_candidates
from app.discovery.prioritization import prioritize_for_research, select_for_verification
from app.discovery.suggestions import suggest_category
from app.sources.catalog import GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID

NOW = datetime(2026, 11, 11, 23, tzinfo=timezone.utc)


def observation(day, text, identity, **kwargs):
    base = Observation(WIKIMEDIA_SOURCE_ID, "wikimedia_aqs", identity, text,
                       NOW - timedelta(days=day, hours=2), NOW, "US sample", ("query",),
                       "US_COUNTRY_SCOPED", "SYNTHETIC", "v1")
    return replace(base, **kwargs)


def coverage(source=WIKIMEDIA_SOURCE_ID, mode="SYNTHETIC"):
    return tuple(CoverageDay(source, "US sample", "v1", mode,
                             (NOW - timedelta(days=i)).date(), "COMPLETE", 10, True)
                 for i in range(10))


def keyword(rows, text):
    return next(c for c in extract_candidates(tuple(rows), now=NOW)
                if c.keyword.casefold() == text.casefold())


class DiscoveryTests(unittest.TestCase):
    def test_each_topic_has_explicit_rule_and_unmatched_stays_unclassified(self):
        examples = {
            "fashion": "clothing", "home_living": "furniture", "technology": "software",
            "gaming": "esports", "entertainment": "cinema",
            "celebrity_public_figures": "celebrity", "beauty_personal_care": "skincare",
            "sports": "basketball", "food_beverage": "coffee", "health_fitness": "fitness",
            "travel_lifestyle": "travel", "business_finance": "finance", "news_politics": "elections",
        }
        for category, word in examples.items():
            with self.subTest(category=category):
                candidate = keyword([observation(0, word, category)], word)
                self.assertIn(category, candidate.categories)
                self.assertEqual(candidate.classification_version, "topic-terms-v2")
        self.assertEqual(keyword([observation(0, "quartz", "unknown")], "quartz").categories,
                         ("unclassified",))

    def test_new_phrases_aliases_evidence_and_categories(self):
        rows = (observation(0, "Wide leg linen pants", "1"),
                observation(0, "Wide leg linen pants", "1", query_evidence=("other",)),
                observation(0, "tee shirt", "2", extraction_mode="DIRECT_KEYWORD"),
                observation(0, "t shirt", "3", extraction_mode="DIRECT_KEYWORD"),
                observation(0, "robot coffee", "4"))
        candidates = extract_candidates(rows, now=NOW)
        by_word = {c.keyword.casefold(): c for c in candidates}
        self.assertEqual(len(by_word["wide leg linen pants"].evidence), 2)
        self.assertNotEqual(by_word["tee shirt"].keyword_id, by_word["t shirt"].keyword_id)
        self.assertEqual(by_word["t shirt"].original_forms, ("t shirt",))
        self.assertIn("technology", by_word["robot coffee"].categories)
        self.assertEqual(prioritize_for_research(candidates)[0].categories[0], "fashion")
        self.assertEqual(candidates, extract_candidates(rows, now=NOW))
        self.assertEqual(suggest_category(by_word["robot coffee"]).status, "NO_PRODUCT_SUGGESTION")

    def test_canonical_identity_merges_exact_terms_across_sources_only(self):
        base = observation(0, "Cafe\u0301 &amp; Coffee", "wikimedia", extraction_mode="DIRECT_KEYWORD")
        other_source = replace(
            base, source_id=GOOGLE_SOURCE_ID, provider_id="serpapi_google",
            text="ＣＡＦÉ   & Coffee", market_evidence="US_FILTER_ONLY",
            source_metrics={"provider_metric": None}, source_metric_name="rank",
            source_metric_unit="ordinal position", source_metric_value=None,
        )
        candidates = extract_candidates((base, other_source), now=NOW)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.keyword_id, canonical_keyword_digest("Café & Coffee"))
        self.assertEqual({item.source_id for item in candidate.evidence}, {
            WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID,
        })
        self.assertEqual({item.market_evidence for item in candidate.evidence}, {
            "US_COUNTRY_SCOPED", "US_FILTER_ONLY",
        })
        self.assertEqual({item.source_metric_unit for item in candidate.evidence}, {
            None, "ordinal position",
        })

    def test_models_punctuation_and_related_terms_keep_distinct_identity(self):
        rows = (
            observation(0, "Apple Watch Ultra 2", "watch", extraction_mode="DIRECT_KEYWORD"),
            observation(0, "Apple Pencil Pro", "pencil", extraction_mode="DIRECT_KEYWORD"),
            observation(0, "WH\u20111000XM5", "model-a", extraction_mode="DIRECT_KEYWORD"),
            observation(0, "WH-1000XM5", "model-b", extraction_mode="DIRECT_KEYWORD"),
            observation(0, "C++", "cpp", extraction_mode="DIRECT_KEYWORD"),
            observation(0, "C", "c", extraction_mode="DIRECT_KEYWORD"),
        )
        candidates = extract_candidates(rows, now=NOW)
        by_identity = {item.keyword_id: item for item in candidates}
        self.assertNotEqual(canonical_keyword_digest("Apple Watch Ultra 2"),
                            canonical_keyword_digest("Apple Pencil Pro"))
        normalized_dash = normalize_keyword_text("WH\u20111000XM5")[0]
        self.assertEqual(by_identity[canonical_keyword_digest("WH-1000XM5")].original_forms,
                         tuple(sorted(("WH-1000XM5", normalized_dash))))
        self.assertNotEqual(canonical_keyword_digest("C++"), canonical_keyword_digest("C"))

    def test_emerging_with_complete_sample_and_budget(self):
        candidate = keyword([observation(0, "linen pants alpha", "1"),
                             observation(1, "linen pants beta", "2")], "linen pants")
        result = assess_candidate(candidate, coverage(), as_of=NOW.date(), now=NOW)[0]
        self.assertEqual(result.classification, "EMERGING")
        self.assertEqual((result.current_support, result.baseline_support), (2, 0))
        self.assertIsNone(result.growth_ratio)
        self.assertEqual(select_for_verification((candidate,), (result,), last_verified={},
                         now=NOW, max_requests=1, freshness=timedelta(days=1)), (candidate,))
        self.assertEqual(select_for_verification((candidate,), (result,), last_verified={},
                         now=NOW, max_requests=0, freshness=timedelta(days=1)), ())
        self.assertEqual(select_for_verification((candidate,), (result,),
                         last_verified={candidate.keyword_id: NOW}, now=NOW,
                         max_requests=1, freshness=timedelta(days=1)), ())

    def test_missing_coverage_spikes_and_duplicates(self):
        rows = [observation(0, "linen pants alpha", "1"),
                observation(0, "linen pants alpha", "2"),
                observation(0, "linen pants beta", "3")]
        candidate = keyword(rows, "linen pants")
        result = assess_candidate(candidate, coverage(), as_of=NOW.date(), now=NOW)[0]
        self.assertEqual(result.current_support, 2)
        self.assertEqual(result.classification, "INSUFFICIENT_EVIDENCE")
        missing = assess_candidate(candidate, coverage()[:-1], as_of=NOW.date(), now=NOW)[0]
        self.assertIsNone(missing.baseline_support)
        self.assertEqual(missing.classification, "INSUFFICIENT_EVIDENCE")

    def test_acceleration_and_source_partition(self):
        rows = [observation(0, "linen pants alpha", "1"),
                observation(1, "linen pants beta", "2"),
                observation(5, "linen pants gamma", "3"),
                observation(0, "linen pants delta", "4", source_id=GOOGLE_SOURCE_ID,
                            provider_id="serpapi_google", market_evidence="US_FILTER_ONLY")]
        candidate = keyword(rows, "linen pants")
        results = assess_candidate(candidate, coverage() + coverage(GOOGLE_SOURCE_ID),
                                   as_of=NOW.date(), now=NOW)
        self.assertEqual(len(results), 2)
        wiki = next(x for x in results if x.source_id == WIKIMEDIA_SOURCE_ID)
        self.assertEqual(wiki.classification, "ACCELERATING")
        self.assertEqual(wiki.current_support, 2)

    def test_live_requires_explicit_current_grants(self):
        live = observation(0, "linen pants", "1", data_mode="LIVE")
        with self.assertRaises(PermissionError):
            extract_candidates((live,), now=NOW)
        grant = ProcessingGrant(WIKIMEDIA_SOURCE_ID, True, False, NOW + timedelta(days=1), "v1")
        candidate = next(c for c in extract_candidates((live,), grants=(grant,), now=NOW)
                         if c.keyword == "linen pants")
        with self.assertRaises(PermissionError):
            assess_candidate(candidate, coverage(mode="LIVE"), as_of=NOW.date(), now=NOW,
                             grants=(grant,))
        with self.assertRaises(PermissionError):
            extract_candidates((live,), grants=(replace(grant, expires_at=NOW),), now=NOW)

    def test_unknown_current_geography_cannot_borrow_historical_us_evidence(self):
        rows = [observation(0, "linen pants alpha", "1", market_evidence="UNKNOWN"),
                observation(1, "linen pants beta", "2", market_evidence="UNKNOWN"),
                observation(5, "linen pants gamma", "3")]
        candidate = keyword(rows, "linen pants")
        result = assess_candidate(candidate, coverage(), as_of=NOW.date(), now=NOW)[0]
        self.assertEqual(result.classification, "ACCELERATING")
        self.assertEqual(result.market_evidence, "UNKNOWN")
        self.assertEqual(select_for_verification((candidate,), (result,), last_verified={},
                         now=NOW, max_requests=1, freshness=timedelta(days=1)), ())

    def test_cached_evidence_preserves_synthetic_origin(self):
        candidate = keyword([observation(0, "linen pants alpha", "1", delivery_mode="CACHED"),
                             observation(1, "linen pants beta", "2")], "linen pants")
        self.assertEqual(candidate.evidence[0].data_mode, "SYNTHETIC")
        result = assess_candidate(candidate, coverage(), as_of=NOW.date(), now=NOW)[0]
        self.assertEqual(result.data_mode, "SYNTHETIC")
        self.assertIn("INCLUDES_CACHED_EVIDENCE", result.limitations)

    def test_reject_bad_input_and_credentials_in_url(self):
        for bad in (observation(0, "hello", "1", observed_at=datetime(2026, 1, 1)),
                    observation(0, "hello", "1", source_url="https://example.com/?api_key=x"),
                    observation(0, "word " * 101, "1")):
            with self.assertRaises(ValueError):
                extract_candidates((bad,), now=NOW)


if __name__ == "__main__":
    unittest.main()
