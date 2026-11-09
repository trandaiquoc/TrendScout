import json
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import httpx

from app.adapters.wikimedia import WikimediaAdapter, observation_status
from app.models.observations import FetchStatus, ObservationStatus

DAY = date(2026, 11, 6)


def payload(day=DAY, articles=None):
    return {"items": [{"country": "US", "access": "all-access", "year": day.strftime("%Y"),
                        "month": day.strftime("%m"), "day": day.strftime("%d"),
                        "articles": articles if articles is not None else [
                            {"article": "Red_Shirt", "project": "en.wikipedia", "views_ceil": 0, "rank": 1},
                            {"article": "Red_Shirt", "project": "fr.wikipedia", "views_ceil": 100, "rank": 2}]}]}


class WikimediaTests(unittest.IsolatedAsyncioTestCase):
    async def run_day(self, handler, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await WikimediaAdapter(client, max_retry_wait=0, **kwargs).fetch_day(DAY)

    async def test_valid_full_response_and_source_identity(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=payload())
        snap, rows = await self.run_day(handler)
        self.assertEqual(snap.fetch_status, FetchStatus.SUCCESS)
        self.assertEqual((snap.item_count, snap.rejected_count, snap.request_count), (2, 0, 1))
        self.assertEqual(rows[0].canonical_keyword, "Red Shirt")
        self.assertEqual((rows[0].views_ceil, rows[0].rank), (0, 1))
        self.assertEqual(rows[0].raw_record["article"], "Red_Shirt")
        self.assertEqual(rows[0].to_canonical()["rank"], 1)
        self.assertEqual(rows[0].source_url, "https://en.wikipedia.org/wiki/Red_Shirt")
        self.assertEqual(observation_status(snap, rows, "fr.wikipedia", "Red_Shirt"), ObservationStatus.OBSERVED)
        self.assertEqual(observation_status(snap, rows, "de.wikipedia", "Red_Shirt"), ObservationStatus.NOT_IN_TOP)
        self.assertEqual(requests[0].url.path, "/api/rest_v1/metrics/pageviews/top-per-country/US/all-access/2026/11/06")
        self.assertEqual(requests[0].url.query, b"")
        self.assertIn("https://github.com/trandaiquoc/TrendScout", requests[0].headers["User-Agent"])

    async def test_single_component_project_and_unknown_domain(self):
        data = payload(articles=[{"article": "MediaWiki", "project": "mediawiki", "views_ceil": 10, "rank": 1},
                                 {"article": "Page", "project": "unknown", "views_ceil": 15, "rank": 2}])
        snap, rows = await self.run_day(lambda req: httpx.Response(200, json=data))
        self.assertEqual(snap.fetch_status, FetchStatus.SUCCESS)
        self.assertEqual(rows[0].source_url, "https://mediawiki.org/wiki/MediaWiki")
        self.assertIsNone(rows[1].source_url)
        self.assertIn("UNVERIFIED_PROJECT_URL", rows[1].quality_flags)

    async def test_partial_and_missing_not_zero(self):
        data = payload(articles=[{"article": "Good", "project": "en.wikipedia", "views_ceil": 0, "rank": 1},
                                 {"article": "Bad", "project": "en.wikipedia", "rank": 2}])
        snap, rows = await self.run_day(lambda req: httpx.Response(200, json=data))
        self.assertEqual((snap.fetch_status, snap.item_count, snap.rejected_count, len(rows)), (FetchStatus.PARTIAL, 2, 1, 1))
        self.assertEqual(snap.rejected_records, (data["items"][0]["articles"][1],))
        self.assertEqual(observation_status(snap, rows, "en.wikipedia", "Bad"), ObservationStatus.FETCH_ERROR)
        self.assertEqual(observation_status(snap, rows, "en.wikipedia", "Good"), ObservationStatus.OBSERVED)
        self.assertEqual(rows[0].views_ceil, 0)

    async def test_missing_rows_and_date_schema_are_errors(self):
        cases = [payload(articles=[{"article": "Bad", "project": "en.wikipedia", "rank": 1}]),
                 {"items": [{"country": "US", "access": "all-access", "year": "2026", "month": "13", "day": "06", "articles": []}]},
                 {"items": [{"country": "US", "access": "all-access"}]}]
        for case in cases:
            snap, _ = await self.run_day(lambda req: httpx.Response(200, json=case))
            self.assertEqual(snap.fetch_status, FetchStatus.FETCH_ERROR)
            self.assertIsNotNone(snap.error)

    async def test_404_and_invalid_json(self):
        snap, rows = await self.run_day(lambda req: httpx.Response(404))
        self.assertEqual(snap.fetch_status, FetchStatus.DAY_UNAVAILABLE)
        self.assertIsNone(snap.item_count)
        self.assertEqual(observation_status(snap, rows, "en.wikipedia", "x"), ObservationStatus.DAY_UNAVAILABLE)
        snap, _ = await self.run_day(lambda req: httpx.Response(200, text="not-json"))
        self.assertEqual(snap.fetch_status, FetchStatus.FETCH_ERROR)
        self.assertIsNone(snap.item_count)

    async def test_429_retry_after_and_too_long(self):
        count = 0
        def handler(req):
            nonlocal count
            count += 1
            return httpx.Response(429, headers={"Retry-After": "0"}) if count == 1 else httpx.Response(200, json=payload())
        with patch("app.adapters.wikimedia.asyncio.sleep", new_callable=AsyncMock) as sleep:
            snap, _ = await self.run_day(handler)
            sleep.assert_awaited_once_with(0.0)
        self.assertEqual((snap.fetch_status, snap.request_count), (FetchStatus.SUCCESS, 2))
        snap, _ = await self.run_day(lambda req: httpx.Response(429, headers={"Retry-After": "100"}))
        self.assertEqual((snap.fetch_status, snap.request_count), (FetchStatus.FETCH_ERROR, 1))
        self.assertIn("exceeds", snap.error)

    async def test_timeout_and_5xx_retry_bounded(self):
        def timeout(req):
            raise httpx.ReadTimeout("timeout", request=req)
        snap, _ = await self.run_day(timeout)
        self.assertEqual((snap.fetch_status, snap.request_count), (FetchStatus.FETCH_ERROR, 3))
        snap, _ = await self.run_day(lambda req: httpx.Response(503))
        self.assertEqual((snap.fetch_status, snap.request_count), (FetchStatus.FETCH_ERROR, 1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503))) as client:
            with patch("app.adapters.wikimedia.asyncio.sleep", new_callable=AsyncMock) as sleep:
                snap, _ = await WikimediaAdapter(client, max_retries=2).fetch_day(DAY)
        self.assertEqual((snap.fetch_status, snap.request_count), (FetchStatus.FETCH_ERROR, 3))
        self.assertEqual(sleep.await_count, 2)

    async def test_lookback_then_calendar_history(self):
        requests = []
        def handler(req):
            day = date.fromisoformat(str(req.url.path)[-10:].replace("/", "-"))
            requests.append(day)
            return httpx.Response(404) if day > DAY else httpx.Response(200, json=payload(day))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WikimediaAdapter(client, max_retry_wait=0).collect(date(2026, 11, 8), history_days=2)
        self.assertEqual(result.analysis_date, DAY)
        self.assertEqual(result.request_count, 5)
        self.assertEqual(requests[-2:], [date(2026, 11, 5), date(2026, 11, 4)])

    async def test_fetch_error_does_not_fallback(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(500))) as client:
            result = await WikimediaAdapter(client, max_retries=0).collect(DAY)
        self.assertIsNone(result.analysis_date)
        self.assertEqual(result.request_count, 1)
