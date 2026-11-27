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

NOW = datetime(2026, 11, 11, 10, tzinfo=timezone.utc)


def observation(day, text, identity, **kwargs):
    base = Observation(WIKIMEDIA_SOURCE_ID, "wikimedia_aqs", identity, text,
                       NOW - timedelta(days=day, hours=2), NOW, "US sample", ("query",),
                       "US_COUNTRY_SCOPED", "SYNTHETIC", "v1")
    return replace(base, **kwargs)


def coverage(source=WIKIMEDIA_SOURCE_ID, mode="SYNTHETIC", *, days=7,
             exposures=None, status="COMPLETE", absence_observable=True,
             collected_at=NOW):
    from app.retention.policy import effective_retention_deadline

    expires_at = effective_retention_deadline(collected_at)
    return tuple(CoverageDay(
        source, "US sample", "v1", mode,
        (NOW - timedelta(days=i)).date(), status,
        (exposures or {}).get((NOW - timedelta(days=i)).date(), 10), absence_observable,
        collected_at, expires_at,
    ) for i in range(days))


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
        self.assertEqual(
            [item.keyword_id for item in prioritize_for_research(candidates)],
            sorted(item.keyword_id for item in candidates),
        )
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
        self.assertEqual(result.algorithm_version, "sample-presence-v2")
        self.assertEqual(select_for_verification((candidate,), (result,), last_verified={},
                         now=NOW, max_requests=1, freshness=timedelta(days=1)), (candidate,))
        self.assertEqual(select_for_verification((candidate,), (result,), last_verified={},
                         now=NOW, max_requests=0, freshness=timedelta(days=1)), ())
        self.assertEqual(select_for_verification((candidate,), (result,),
                         last_verified={candidate.keyword_id: NOW}, now=NOW,
                         max_requests=1, freshness=timedelta(days=1)), ())

    def test_global_assessment_is_not_relabelled_or_selected_for_us_verification(self):
        candidate = keyword([replace(
            observation(0, "solar generator alpha", "global"),
            market_evidence="GLOBAL", scope="Global sample",
        )], "solar generator")
        result = assess_candidate(candidate, coverage(), as_of=NOW.date(), now=NOW)[0]
        self.assertEqual(result.market_evidence, "GLOBAL")
        eligible = replace(result, classification="EMERGING", current_support=2)
        self.assertEqual(select_for_verification(
            (candidate,), (eligible,), last_verified={}, now=NOW,
            max_requests=1, freshness=timedelta(days=1),
        ), ())

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
                observation(2, "linen pants current gamma", "3"),
                observation(3, "linen pants baseline delta", "4"),
                observation(6, "linen pants baseline epsilon", "5"),
                observation(0, "linen pants delta", "4", source_id=GOOGLE_SOURCE_ID,
                            provider_id="serpapi_google", market_evidence="US_FILTER_ONLY")]
        candidate = keyword(rows, "linen pants")
        baseline_exposure = {
            (NOW - timedelta(days=day)).date(): 20 for day in (3, 4, 5, 6)
        }
        results = assess_candidate(candidate, coverage(exposures=baseline_exposure) +
                                   coverage(GOOGLE_SOURCE_ID, exposures=baseline_exposure),
                                   as_of=NOW.date(), now=NOW)
        self.assertEqual(len(results), 2)
        wiki = next(x for x in results if x.source_id == WIKIMEDIA_SOURCE_ID)
        self.assertEqual(wiki.classification, "ACCELERATING")
        self.assertEqual(wiki.current_support, 3)

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
        self.assertEqual(result.classification, "INSUFFICIENT_EVIDENCE")
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

    def test_fresh_start_requires_all_seven_retained_coverage_days(self):
        candidate = keyword([
            observation(0, "linen pants current alpha", "current-1"),
            observation(1, "linen pants current beta", "current-2"),
            observation(2, "linen pants current gamma", "current-3"),
            observation(3, "linen pants baseline delta", "baseline-1"),
            observation(6, "linen pants baseline epsilon", "baseline-2"),
        ], "linen pants")
        for available_days in (0, 1, 3, 6):
            with self.subTest(available_days=available_days):
                result = assess_candidate(candidate, coverage(days=available_days),
                                          as_of=NOW.date(), now=NOW)[0]
                self.assertEqual(result.classification, "INSUFFICIENT_EVIDENCE")
        baseline_exposure = {
            (NOW - timedelta(days=day)).date(): 5 for day in (3, 4, 5, 6)
        }
        complete = assess_candidate(candidate, coverage(exposures=baseline_exposure),
                                    as_of=NOW.date(), now=NOW)[0]
        self.assertEqual(complete.classification, "SUSTAINED")
        self.assertEqual(complete.growth_ratio, 1.0)

    def test_normalized_rates_and_minimum_support_guard(self):
        candidate = keyword([
            *(observation(day, f"linen pants current {day}", f"c{day}")
              for day in (0, 1, 2)),
            *(observation(day, f"linen pants baseline {day}", f"b{day}")
              for day in (3, 4, 5, 6)),
        ], "linen pants")
        exposures = {
            **{(NOW - timedelta(days=day)).date(): 20 for day in (3, 4, 5, 6)},
        }
        result = assess_candidate(candidate, coverage(exposures=exposures),
                                  as_of=NOW.date(), now=NOW)[0]
        self.assertEqual(result.classification, "ACCELERATING")
        self.assertAlmostEqual(result.growth_ratio, 2.0)

        weak_baseline = keyword([
            observation(0, "linen pants current alpha", "current-1"),
            observation(1, "linen pants current beta", "current-2"),
            observation(3, "linen pants only baseline", "baseline-1"),
        ], "linen pants")
        insufficient = assess_candidate(weak_baseline, coverage(),
                                        as_of=NOW.date(), now=NOW)[0]
        self.assertEqual(insufficient.classification, "INSUFFICIENT_EVIDENCE")
        self.assertIsNone(insufficient.growth_ratio)

    def test_partial_censored_missing_or_expired_coverage_fails_closed(self):
        candidate = keyword([
            observation(0, "linen pants alpha", "1"),
            observation(1, "linen pants beta", "2"),
        ], "linen pants")
        partial = list(coverage())
        partial[3] = replace(partial[3], status="PARTIAL")
        censored = list(coverage())
        censored[3] = replace(censored[3], absence_observable_in_sample=False)
        old_collection = NOW - timedelta(days=8)
        expired = coverage(collected_at=old_collection)
        for rows in (tuple(partial), tuple(censored), expired):
            result = assess_candidate(candidate, rows, as_of=NOW.date(), now=NOW)[0]
            self.assertEqual(result.classification, "INSUFFICIENT_EVIDENCE")

    def test_collection_expiry_does_not_rewrite_provider_observation_date(self):
        from app.discovery.source_results import discover_fetch_results
        from app.retention.policy import rolling_retention_window

        utc_late = datetime(2026, 11, 11, 23, tzinfo=timezone.utc)
        local_day = rolling_retention_window(utc_late).business_date
        record = {
            "article_display": "Linen pants",
            "article_original": "Linen_pants",
            "project": "en.wikipedia",
            "observation_date": "2026-11-11",
            "fetched_at": utc_late.isoformat(),
            "country_scope": "US",
            "access": "all-access",
        }
        result = discover_fetch_results([{
            "source_id": WIKIMEDIA_SOURCE_ID,
            "data_mode": "SYNTHETIC",
            "records": [record],
        }], now=utc_late)
        assessment = result["candidates"][0]["assessments"][0]
        reference = result["candidates"][0]["evidence"][0]
        self.assertEqual(assessment["actual_observation_window"]["as_of_date"],
                         local_day.isoformat())
        self.assertEqual(assessment["required_observation_days"], 7)
        self.assertEqual(assessment["available_observation_days"], 1)
        self.assertEqual(reference["observed_at"][:10], "2026-11-11")


if __name__ == "__main__":
    unittest.main()
