import math
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import httpx

from app.adapters.wikimedia import SOURCE, WikimediaAdapter
from app.analysis.keywords import extract_keywords
from app.analysis.trend import DayData, score_trends
from app.models.observations import DatasetSnapshot, FetchStatus, PageObservation
from app.processing.wikimedia_cleaning import clean_snapshot

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def sample(day, title, views, project="en.wikipedia"):
    return PageObservation(source=SOURCE, country_scope="US", access="all-access", observation_date=day,
                           project=project, article_original=title, article_display=title,
                           canonical_keyword=title, views_ceil=views, provider_rank=1, fetched_at=NOW,
                           source_url="https://en.wikipedia.org/wiki/Example", raw_record={"article": title})


def daily(day, rows=(), status=FetchStatus.SUCCESS, raw=None):
    snap = DatasetSnapshot(source=SOURCE, country_scope="US", access="all-access", observation_date=day,
                           fetched_at=NOW, fetch_status=status, http_status=404 if status == FetchStatus.DAY_UNAVAILABLE else 200,
                           item_count=len(rows), request_url="url", rejected_count=0)
    return DayData(snap, tuple(extract_keywords(rows)), tuple(rows if raw is None else raw))


def generate(current, history, project="en.wikipedia"):
    days = [daily(DAY, [sample(DAY, "Article", current, project)])]
    for offset, value in enumerate(history, 1):
        day = DAY - timedelta(days=offset)
        if value is None:
            days.append(daily(day, [], FetchStatus.DAY_UNAVAILABLE))
        else:
            days.append(daily(day, [sample(day, "Article", value, project)]))
    return days


class TrendTests(unittest.TestCase):
    def test_growth_stable_falling_high_volume(self):
        for current, history, expected in [(800, [100]*7, "EMERGING"), (100, [100]*7, "STABLE"),
                                           (50, [100]*7, "FALLING"), (100000, [100000]*7, "STABLE")]:
            result = score_trends(DAY, generate(current, history))[0]
            self.assertEqual(result.status, expected)
            self.assertEqual(result.observed_baseline_days, 7)
            self.assertGreaterEqual(result.score, 0)
            self.assertLessEqual(result.score, 100)
        self.assertEqual(score_trends(DAY, generate(100000, [100000]*7))[0].score, 30)

    def test_formula_and_maximum_use_all_current_entities(self):
        days = generate(400, [100]*7)
        other = sample(DAY, "Popular", 1000)
        days[0] = daily(DAY, [sample(DAY, "Article", 400), other])
        result = score_trends(DAY, days)[0]
        g = math.log2(4) / 3
        v = math.log1p(400) / math.log1p(1000)
        self.assertEqual(result.score, round(100 * (0.7*g + 0.3*v)))
        self.assertEqual(result.score_components["max_views_ceil"], 1000)
        self.assertEqual(score_trends(DAY, days), score_trends(DAY, days))

    def test_insufficient_baseline_zero_current_and_invalid(self):
        for current, history in [(100, [100, 100]), (100, [0]*7), (0, [100]*7), (None, [100]*7),
                                 (float("nan"), [100]*7), (float("inf"), [100]*7), (-1, [100]*7)]:
            result = score_trends(DAY, generate(current, history))[0]
            self.assertEqual(result.status, "INSUFFICIENT_DATA")
            self.assertIsNone(result.score)
            self.assertIsNone(result.ratio)
        result = score_trends(DAY, generate(100, [0]*7))[0]
        self.assertEqual(result.baseline, 0)
        self.assertEqual(result.observed_baseline_days, 7)

    def test_missing_censored_unavailable_invalid_and_rejected(self):
        days = generate(100, [100, 100, 100])
        d4, d5, d6, d7 = (DAY-timedelta(days=i) for i in range(4, 8))
        days.extend([daily(d4, []), daily(d5, [], FetchStatus.DAY_UNAVAILABLE),
                     daily(d6, [], FetchStatus.FETCH_ERROR), daily(d7, [], raw=[sample(d7, "Article", 100)])])
        result = score_trends(DAY, days)[0]
        self.assertEqual(result.observed_baseline_days, 3)
        self.assertEqual(dict(result.missing_baseline_dates), {d4: "NOT_IN_TOP", d5: "DAY_UNAVAILABLE", d6: "FETCH_ERROR", d7: "REJECTED_BY_CLEANING"})
        self.assertIn("INCOMPLETE_BASELINE_COVERAGE", result.quality_flags)
        days[1] = daily(DAY-timedelta(days=1), [sample(DAY-timedelta(days=1), "Article", float("nan"))])
        result = score_trends(DAY, days)[0]
        self.assertEqual(result.status, "INSUFFICIENT_DATA")
        self.assertIn("INVALID_VALUE", dict(result.missing_baseline_dates).values())

    def test_projects_are_not_merged(self):
        days = []
        for offset in range(8):
            day = DAY - timedelta(days=offset)
            days.append(daily(day, [sample(day, "Cat", 800 if offset == 0 else 100), sample(day, "Cat", 1000, "fr.wikipedia")]))
        results = {r.project: r for r in score_trends(DAY, days)}
        self.assertEqual(results["en.wikipedia"].status, "EMERGING")
        self.assertEqual(results["fr.wikipedia"].status, "STABLE")
        self.assertEqual(results["en.wikipedia"].baseline, 100)
        self.assertEqual(results["fr.wikipedia"].baseline, 1000)

    def test_country_scope_has_separate_baseline_and_maximum(self):
        days = generate(800, [100]*7)
        for offset in range(8):
            day = DAY - timedelta(days=offset)
            canadian_row = replace(sample(day, "Article", 50 if offset == 0 else 1000), country_scope="CA")
            original = daily(day, [])
            days.append(DayData(replace(original.snapshot, country_scope="CA"),
                                tuple(extract_keywords([canadian_row])), (canadian_row,)))
        results = {r.country_scope: r for r in score_trends(DAY, days)}
        self.assertEqual(results["US"].baseline, 100)
        self.assertEqual(results["US"].status, "EMERGING")
        self.assertEqual(results["CA"].baseline, 1000)
        self.assertEqual(results["CA"].status, "FALLING")
        self.assertEqual(results["CA"].score_components["max_views_ceil"], 50)

    def test_partial_snapshot_and_duplicate_are_not_not_in_top(self):
        days = generate(100, [100]*3)
        day = DAY-timedelta(days=4)
        days.append(daily(day, [], FetchStatus.PARTIAL))
        result = score_trends(DAY, days)[0]
        self.assertEqual(dict(result.missing_baseline_dates)[day], "PARTIAL_SNAPSHOT")
        dupe_day = DAY-timedelta(days=1)
        days[1] = daily(dupe_day, [sample(dupe_day, "Article", 100), sample(dupe_day, "Article", 200)])
        self.assertEqual(dict(score_trends(DAY, days)[0].missing_baseline_dates)[dupe_day], "DUPLICATE_OBSERVATION")


class TrendIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_clean_keyword_scoring(self):
        def handler(request):
            day = date.fromisoformat(request.url.path[-10:].replace("/", "-"))
            item = {"country": "US", "access": "all-access", "year": day.strftime("%Y"), "month": day.strftime("%m"), "day": day.strftime("%d"),
                    "articles": [{"article": "Paris_Fashion_Week", "project": "en.wikipedia", "rank": 1, "views_ceil": 800 if day == DAY else 100},
                                 {"article": "Special:Search", "project": "en.wikipedia", "rank": 2, "views_ceil": 900}]}
            return httpx.Response(200, json={"items": [item]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = WikimediaAdapter(client)
            days = []
            for offset in range(8):
                snapshot, rows = await adapter.fetch_day(DAY - timedelta(days=offset))
                cleaned = clean_snapshot(snapshot, rows)
                days.append(DayData(snapshot, tuple(extract_keywords(cleaned.accepted)), tuple(rows)))
        result = score_trends(DAY, days)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, "EMERGING")
        self.assertEqual(result[0].score, 100)
        self.assertEqual(result[0].categories, ("fashion",))
        self.assertEqual(result[0].score_components["max_views_ceil"], 800)
