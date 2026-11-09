import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

import httpx

from app.adapters.wikimedia import WikimediaAdapter
from app.analysis.keywords import CLASSIFICATION_VERSION, extract_keyword, extract_keywords, filter_keywords
from app.models.observations import PageObservation
from app.processing.wikimedia_cleaning import clean_snapshot

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def observation(title, *, project="en.wikipedia", day=DAY):
    original = title.replace(" ", "_")
    return PageObservation(source="wikimedia", country_scope="US", access="all-access", observation_date=day,
                           project=project, article_original=original, article_display=title,
                           canonical_keyword=title, views_ceil=10, provider_rank=1, fetched_at=NOW,
                           source_url="https://en.wikipedia.org/wiki/X", quality_flags=("TOP_LIST_ONLY",),
                           raw_record={"article": original, "project": project, "views_ceil": 10, "rank": 1})


class KeywordTests(unittest.TestCase):
    def test_fashion_and_home_evidence(self):
        cases = [("Paris Fashion Week", "fashion", "fashion_industry"),
                 ("History of the washing machine", "home_goods", "home_products")]
        for title, category, rule in cases:
            result = extract_keyword(observation(title))
            self.assertEqual(result.categories, (category,))
            self.assertEqual(result.classification_version, CLASSIFICATION_VERSION)
            self.assertTrue(any(e.rule_id == rule and e.category == category for e in result.classification_evidence))

    def test_other_unknown_and_ambiguous(self):
        self.assertEqual(extract_keyword(observation("2024 United States presidential election")).categories, ("other",))
        for title in ("Home", "Fashion", "Dress", "Designer", "Brand", "Jordan", "Apple", "Red Carpet"):
            result = extract_keyword(observation(title))
            self.assertEqual(result.categories, ("unclassified",), title)
            self.assertEqual(result.classification_evidence, ())
        self.assertEqual(extract_keyword(observation("The Washing Machine (film)")).categories, ("other",))
        self.assertEqual(extract_keyword(observation("Fashion Week (song)")).categories, ("unclassified",))

    def test_multiple_labels_unicode_and_original(self):
        source = observation("Café fashion week and washing machine")
        result = extract_keyword(source)
        self.assertEqual(result.categories, ("fashion", "home_goods"))
        self.assertEqual(result.search_keyword, "café fashion week and washing machine")
        self.assertEqual(result.canonical_keyword, source.canonical_keyword)
        self.assertEqual(result.article_original, source.article_original)
        self.assertEqual(result.raw_record, source.raw_record)
        self.assertEqual(result.to_record()["observation_date"], "2026-11-06")
        self.assertEqual(len(result.to_record()["classification_evidence"]), 2)
        self.assertEqual(result.quality_flags[0], "TOP_LIST_ONLY")

    def test_identity_by_project_and_source_but_observation_by_day_and_scope(self):
        one = observation("Cat")
        next_day = replace(one, observation_date=date(2026, 11, 5))
        different_project = replace(one, project="fr.wikipedia")
        a, b, c = extract_keywords([one, next_day, different_project])
        self.assertEqual(a.keyword_id, b.keyword_id)
        self.assertNotEqual(a.observation_id, b.observation_id)
        self.assertNotEqual(a.keyword_id, c.keyword_id)
        self.assertEqual(len({a.observation_id, b.observation_id, c.observation_id}), 3)

    def test_all_filter_and_empty_reason(self):
        results = extract_keywords([observation("Paris Fashion Week"), observation("Apple")])
        self.assertEqual(filter_keywords(results).items, tuple(results))
        self.assertEqual(len(filter_keywords(results, "fashion").items), 1)
        self.assertEqual(filter_keywords(results, "home_goods").items, ())
        self.assertEqual(filter_keywords(results, "home_goods").empty_reason, "NO_MATCHES_IN_ANALYZED_DATA")
        with self.assertRaises(ValueError):
            filter_keywords(results, "invalid")


class PipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_clean_and_classify_preserves_all_valid(self):
        body = {"items": [{"country": "US", "access": "all-access", "year": "2026", "month": "11", "day": "06",
                           "articles": [
                               {"article": "Main_Page", "project": "en.wikipedia", "views_ceil": 10, "rank": 1},
                               {"article": "Paris_Fashion_Week", "project": "en.wikipedia", "views_ceil": 20, "rank": 2},
                               {"article": "Vacuum_cleaner", "project": "en.wikipedia", "views_ceil": 0, "rank": 3},
                               {"article": "Cat", "project": "en.wikipedia", "views_ceil": 30, "rank": 4},
                               {"article": "Cat", "project": "fr.wikipedia", "views_ceil": 40, "rank": 5}]}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))) as client:
            snapshot, rows = await WikimediaAdapter(client).fetch_day(DAY)
        cleaned = clean_snapshot(snapshot, rows)
        results = extract_keywords(cleaned.accepted)
        self.assertEqual((snapshot.item_count, cleaned.report.accepted_count, cleaned.report.rejected_count), (5, 4, 1))
        self.assertEqual(len(results), 4)
        self.assertEqual(len(filter_keywords(results).items), 4)
        self.assertEqual([r.categories for r in results], [("fashion",), ("home_goods",), ("unclassified",), ("unclassified",)])
        self.assertEqual(results[1].views_ceil, 0)
        self.assertNotEqual(results[-1].keyword_id, results[-2].keyword_id)
