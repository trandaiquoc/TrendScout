import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

from app.models.observations import DatasetSnapshot, FetchStatus, PageObservation
from app.processing.wikimedia_cleaning import clean_snapshot

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def row(title="Example", project="en.wikipedia", **changes):
    value = PageObservation(source="wikimedia", country_scope="US", access="all-access", observation_date=DAY,
                            project=project, article_original=title, article_display=title, views_ceil=100,
                            provider_rank=1, fetched_at=NOW, source_url="https://en.wikipedia.org/wiki/Example",
                            raw_record={"article": title, "project": project, "rank": 1, "views_ceil": 100})
    return replace(value, **changes)


def snapshot(count, *, status=FetchStatus.SUCCESS, rejected=0, day=DAY):
    return DatasetSnapshot(source="wikimedia", country_scope="US", access="all-access", observation_date=day,
                           fetched_at=NOW, fetch_status=status, http_status=200 if count is not None else 404,
                           item_count=count, request_url="url", rejected_count=rejected)


class CleaningTests(unittest.TestCase):
    def test_unicode_percent_spacing_and_raw_preserved(self):
        original = row("  Cafe%CC%81__%E2%98%95  ")
        result = clean_snapshot(snapshot(1), [original])
        cleaned = result.accepted[0]
        self.assertEqual(cleaned.canonical_keyword, "Café ☕")
        for field in ("article_original", "project", "observation_date", "views_ceil", "provider_rank", "source_url", "raw_record"):
            self.assertEqual(getattr(cleaned, field), getattr(original, field))
        self.assertEqual(clean_snapshot(snapshot(1), [row("C++_100%")]).accepted[0].canonical_keyword, "C++ 100%")
        self.assertEqual(clean_snapshot(snapshot(1), [row("%2520")]).accepted[0].canonical_keyword, "%20")

    def test_technical_and_titles_with_colons(self):
        titles = ["Main_Page", "Special:Search", "Spécial:Recherche", "File:Foo", "-", "Star_Trek:_Discovery", "Special_Relativity", "Main_Pageant"]
        result = clean_snapshot(snapshot(len(titles)), [row(title) for title in titles])
        self.assertEqual([x.article_original for x in result.accepted], titles[5:])
        self.assertEqual(result.report.rejection_reasons, {"TECHNICAL_MAIN_PAGE": 1, "TECHNICAL_NAMESPACE": 3, "TECHNICAL_PLACEHOLDER": 1})

    def test_duplicates_are_scoped_to_project_and_day(self):
        rows = [row("Cat"), row("Cat"), row("Cat", "fr.wikipedia")]
        result = clean_snapshot(snapshot(3), rows)
        self.assertEqual((result.report.accepted_count, result.report.duplicate_count), (2, 1))
        other_day = date(2026, 11, 5)
        second = replace(row("Cat"), observation_date=other_day)
        self.assertEqual(clean_snapshot(snapshot(1, day=other_day), [second]).report.accepted_count, 1)
        self.assertEqual(result.duplicates[0], rows[1])

    def test_zero_missing_invalid_and_bad_time(self):
        rows = [row("Zero", views_ceil=0), row("Missing", views_ceil=None), row("NoRank", provider_rank=None),
                row("NoProject", project=""), row("BadDate", observation_date="not-a-date"),
                row("BadTime", fetched_at=datetime(2026, 1, 1)), row("BadRaw", raw_record=None), row("BadEncoding%FF")]
        result = clean_snapshot(snapshot(len(rows)), rows)
        self.assertEqual(result.accepted[0].views_ceil, 0)
        self.assertEqual(result.report.accepted_count, 1)
        self.assertEqual(result.report.rejected_count, 7)
        self.assertEqual(set(result.report.rejection_reasons), {"MISSING_VIEWS", "INVALID_RANK", "MISSING_REQUIRED_FIELD", "INVALID_OBSERVATION_DATE", "INVALID_FETCH_TIMESTAMP", "MISSING_RAW_RECORD", "INVALID_TITLE_ENCODING"})

    def test_unavailable_partial_and_reconciliation(self):
        result = clean_snapshot(snapshot(None, status=FetchStatus.DAY_UNAVAILABLE, rejected=None), [])
        self.assertIsNone(result.report.raw_count)
        self.assertIsNone(result.report.accepted_count)
        partial = clean_snapshot(snapshot(3, status=FetchStatus.PARTIAL, rejected=1), [row("Cat"), row("Cat")])
        self.assertEqual((partial.report.raw_count, partial.report.accepted_count, partial.report.rejected_count, partial.report.duplicate_count), (3, 1, 1, 1))
        self.assertEqual(partial.report.rejection_reasons, {"ADAPTER_INVALID": 1})
        with self.assertRaises(ValueError):
            clean_snapshot(snapshot(5), [row()])
