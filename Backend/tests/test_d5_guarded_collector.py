"""Offline integration of source pagination, budget and normalized evidence."""

import tempfile
import unittest
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
from test_d5_live_gateway import FAKE_SECRET, Approvals, Credentials
from test_d5_public_admission import NOW, coordinator, policy

from app.jobs.source_checkpoints import CheckpointGrant, SourceCheckpointJournal
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.guarded_collector import (
    CollectionLimits,
    GuardedQueryCollector,
    _bounded_checkpoint_records,
)
from app.sources.live_gateway import LivePageGateway
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.workers.guarded_attempt import GuardedAttemptRunner


class GuardedCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = str(Path(self.directory.name) / "ledger.sqlite")
        self.control = coordinator(path, policy(plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint))
        self.job = self.control.admit("public-fetch-idempotency", now=NOW)["job_id"]
        self.fence = self.control.claim(self.job, "worker", now=NOW)["fence"]
        self.runner = GuardedAttemptRunner(self.control, clock=lambda: NOW)
        self.gateways = []

    async def asyncTearDown(self):
        for gateway in self.gateways:
            await gateway.aclose()
        self.directory.cleanup()

    def collector(self, source, handler):
        gateway = LivePageGateway.offline_fixture(source, Credentials(), Approvals(source), self.runner,
            transport=httpx.MockTransport(handler), clock=lambda: NOW)
        self.gateways.append(gateway)
        return GuardedQueryCollector(gateway, utcnow=lambda: NOW)

    async def collect(self, collector, **limits):
        return await collector.collect(plan=DEFAULT_QUERY_PLAN, limits=CollectionLimits(**limits),
                                       job_id=self.job, owner="worker", fence=self.fence)

    @staticmethod
    def search_handler(request):
        start = int(request.url.params["start"])
        page = {"search_metadata": {"status": "Success"},
                "organic_results": [{"position": 1, "link": "https://example.org/item",
                                     "title": "Linen pants" if start == 0 else "Linen trousers"}]}
        if start == 0:
            page["serpapi_pagination"] = {"next": "https://serpapi.com/search.json?" + urlencode({
                "engine": "google", "q": request.url.params["q"], "start": 10,
                "api_key": FAKE_SECRET})}
        return httpx.Response(200, json=page)

    async def test_google_pagination_deduplicates_and_never_returns_key(self):
        result = await self.collect(self.collector(GOOGLE_SOURCE_ID, self.search_handler),
                                    max_queries=1, max_pages_per_query=2, max_requests=2)
        self.assertEqual((result.data_mode, result.status, result.attempted_calls),
                         ("SYNTHETIC", "PARTIAL", 2))
        self.assertEqual(result.queries[0].reason, "END_OF_PAGES")
        self.assertEqual(result.stop_reason, "QUERY_CAP_REACHED")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.records[0].evidence), 2)
        self.assertEqual(result.records[0].data_mode, "SYNTHETIC")
        self.assertNotIn(FAKE_SECRET, repr(result.records))

    async def test_resumes_from_last_durable_page_checkpoint(self):
        calls = []
        def handler(request):
            calls.append(request.url.params["start"])
            return self.search_handler(request)
        collector = self.collector(GOOGLE_SOURCE_ID, handler)
        journal = SourceCheckpointJournal(self.control)
        grant = CheckpointGrant(GOOGLE_SOURCE_ID, "SYNTHETIC", "collector-resume-v1",
                                NOW + timedelta(minutes=5), 300, True)
        revision = [0]
        def interrupt_after_checkpoint(snapshot):
            revision[0] = journal.save(
                self.job, "worker", self.fence, source_id=GOOGLE_SOURCE_ID, grant=grant,
                now=NOW, expected_revision=revision[0],
                snapshot={"schema": 1, "source_id": GOOGLE_SOURCE_ID,
                          "data_mode": "SYNTHETIC", "plan_fingerprint": DEFAULT_QUERY_PLAN.fingerprint,
                          "phase": "RUNNING", "progress": {"collector": snapshot}},
            )
            raise RuntimeError("simulated worker interruption")
        with self.assertRaisesRegex(RuntimeError, "simulated worker interruption"):
            await collector.collect(
                plan=DEFAULT_QUERY_PLAN,
                limits=CollectionLimits(max_queries=1, max_pages_per_query=2, max_requests=2),
                job_id=self.job, owner="worker", fence=self.fence,
                checkpoint=interrupt_after_checkpoint,
            )
        self.assertEqual(calls, ["0"])
        saved = journal.load(self.job, "worker", self.fence, source_id=GOOGLE_SOURCE_ID,
                             grant=grant, now=NOW)
        self.assertEqual(saved["revision"], 1)
        with self.assertRaisesRegex(ValueError, "checkpoint scope mismatch"):
            await collector.collect(
                plan=DEFAULT_QUERY_PLAN,
                limits=CollectionLimits(max_queries=1, max_pages_per_query=2, max_requests=3),
                job_id=self.job, owner="worker", fence=self.fence,
                resume_snapshot=saved["snapshot"]["progress"]["collector"],
            )
        resumed = await collector.collect(
            plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=2, max_requests=2),
            job_id=self.job, owner="worker", fence=self.fence,
            resume_snapshot=saved["snapshot"]["progress"]["collector"],
        )
        self.assertEqual(calls, ["0", "10"])
        self.assertEqual(resumed.queries[0].pages, 2)
        self.assertEqual(len(resumed.records), 1)
        self.assertEqual(len(resumed.records[0].evidence), 2)

    def test_checkpoint_record_budget_reports_omitted_rows(self):
        selected, omitted = _bounded_checkpoint_records([
            {"title": "kept", "body": "x" * 90_000},
            {"title": "omitted", "body": "y" * 30_000},
        ])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["title"], "kept")
        self.assertEqual(omitted, 1)

    async def test_page_and_request_caps_are_distinct(self):
        page_cap = await self.collect(self.collector(GOOGLE_SOURCE_ID, self.search_handler),
                                      max_queries=1, max_pages_per_query=1, max_requests=1)
        self.assertEqual(page_cap.queries[0].reason, "PAGE_CAP_REACHED")
        # Fresh ledger: the prior attempt consumed the same deterministic receipt.
        await self.asyncTearDown()
        await self.asyncSetUp()
        request_cap = await self.collect(self.collector(GOOGLE_SOURCE_ID, self.search_handler),
                                         max_queries=1, max_pages_per_query=2, max_requests=1)
        self.assertEqual(request_cap.queries[0].reason, "REQUEST_BUDGET_EXHAUSTED")
        self.assertEqual(request_cap.status, "PARTIAL")

    async def test_youtube_page_tokens_are_scoped_and_records_deduplicated(self):
        tokens = []
        def handler(request):
            tokens.append(request.url.params.get("pageToken"))
            payload = {"kind": "youtube#searchListResponse", "items": [{
                "kind": "youtube#searchResult", "id": {"kind": "youtube#video", "videoId": "AbCdEf12345"},
                "snippet": {"title": "fixture" if not request.url.params.get("pageToken") else "fixture two",
                            "publishedAt": "2026-11-11T00:00:00Z"}}]}
            if not request.url.params.get("pageToken"):
                payload["nextPageToken"] = "page-two"
            return httpx.Response(200, json=payload)
        # The fixture policy permits only one YouTube request; override with a
        # larger explicitly synthetic internal limit for this isolated test.
        from app.jobs.public_admission import PoolLimit
        self.directory.cleanup()
        self.directory = tempfile.TemporaryDirectory()
        path = str(Path(self.directory.name) / "ledger.sqlite")
        config = policy(plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
                        pools={"serpapi": PoolLimit(2, 5, 2, 5, 1),
                               "youtube": PoolLimit(2, 5, 2, 5, 1)})
        self.control = coordinator(path, config)
        self.job = self.control.admit("public-fetch-idempotency", now=NOW)["job_id"]
        self.fence = self.control.claim(self.job, "worker", now=NOW)["fence"]
        self.runner = GuardedAttemptRunner(self.control, clock=lambda: NOW)
        collector = self.collector(YOUTUBE_SOURCE_ID, handler)
        limits = CollectionLimits(max_queries=1, max_pages_per_query=2, max_requests=2)
        saved = []
        def interrupt_after_checkpoint(snapshot):
            saved.append(snapshot)
            raise RuntimeError("simulated YouTube worker interruption")
        with self.assertRaisesRegex(RuntimeError, "simulated YouTube worker interruption"):
            await collector.collect(plan=DEFAULT_QUERY_PLAN, limits=limits,
                                    job_id=self.job, owner="worker", fence=self.fence,
                                    checkpoint=interrupt_after_checkpoint)
        self.assertEqual(saved[0]["queries"][0]["cursor"], "page-two")
        self.assertEqual(saved[0]["source_id"], YOUTUBE_SOURCE_ID)
        self.assertFalse(saved[0]["queries"][0]["done"])
        result = await collector.collect(plan=DEFAULT_QUERY_PLAN, limits=limits,
                                         job_id=self.job, owner="worker", fence=self.fence,
                                         resume_snapshot=saved[0])
        self.assertEqual(tokens, [None, "page-two"])
        self.assertEqual(result.queries[0].reason, "END_OF_PAGES")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.records[0].query_evidence), 2)
        self.assertEqual(result.records[0].data_mode, "SYNTHETIC")

    async def test_retry_after_is_deferred_when_it_exceeds_deadline(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "9"}, json={"error": "rate limited"})
        result = await self.collect(self.collector(GOOGLE_SOURCE_ID, handler),
                                    max_queries=1, max_requests=1,
                                    max_retries_per_page=1, deadline_seconds=2)
        self.assertEqual(result.stop_reason, "DEFERRED")
        self.assertIsNone(result.data_mode)
        self.assertEqual(result.queries[0].retry_at, NOW + timedelta(seconds=9))
        self.assertEqual(len(calls), 1)

    async def test_replaying_batch_never_reissues_reserved_attempt(self):
        collector = self.collector(GOOGLE_SOURCE_ID, self.search_handler)
        first = await self.collect(collector, max_requests=1)
        second = await self.collect(collector, max_requests=1)
        self.assertEqual(first.queries[0].pages, 1)
        self.assertEqual(second.stop_reason, "ALREADY_RESERVED")

    async def test_bing_uses_provider_next_offset_without_fetching_supplied_link(self):
        seen = []
        def handler(request):
            seen.append(str(request.url.params["first"]))
            first = int(request.url.params["first"])
            result = {"search_metadata": {"status": "Success"},
                      "organic_results": [{"position": 1, "link": "https://example.org/item",
                                           "title": f"Fixture page {first}"}]}
            if first == 1:
                result["serpapi_pagination"] = {"next": "https://serpapi.com/search.json?" + urlencode({
                    "engine": "bing", "q": request.url.params["q"], "first": 6})}
            return httpx.Response(200, json=result)
        result = await self.collect(self.collector(BING_SOURCE_ID, handler), max_queries=1,
                                    max_pages_per_query=2, max_requests=2)
        self.assertEqual(seen, ["1", "6"])
        self.assertEqual(result.queries[0].reason, "END_OF_PAGES")
        self.assertEqual(len(result.records[0].evidence), 2)

    async def test_repeated_provider_page_is_partial(self):
        def handler(request):
            result = {"search_metadata": {"status": "Success"},
                      "organic_results": [{"position": 1, "link": "https://example.org/item",
                                           "title": "Identical"}]}
            if request.url.params["start"] == "0":
                result["serpapi_pagination"] = {"next": "https://serpapi.com/search.json?" + urlencode({
                    "engine": "google", "q": request.url.params["q"], "start": 10})}
            return httpx.Response(200, json=result)
        result = await self.collect(self.collector(GOOGLE_SOURCE_ID, handler), max_queries=1,
                                    max_pages_per_query=2, max_requests=2)
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.stop_reason, "REPEATED_PAGE")

    async def test_rejection_on_earlier_page_keeps_query_partial(self):
        def handler(request):
            first = request.url.params["start"] == "0"
            items = ([{"position": 1, "link": "javascript:alert(1)"}]
                     if first else [{"position": 1, "link": "https://example.org/item"}])
            result = {"search_metadata": {"status": "Success"}, "organic_results": items}
            if first:
                result["serpapi_pagination"] = {"next": "https://serpapi.com/search.json?" + urlencode({
                    "engine": "google", "q": request.url.params["q"], "start": 10})}
            return httpx.Response(200, json=result)
        result = await self.collect(self.collector(GOOGLE_SOURCE_ID, handler), max_queries=1,
                                    max_pages_per_query=2, max_requests=2)
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.queries[0].reason, "REJECTED_RECORDS")
        self.assertEqual(result.queries[0].rejected, 1)

    async def test_retry_spends_new_reservation_and_is_bounded(self):
        calls, delays = [], []
        def handler(request):
            calls.append(request)
            return (httpx.Response(503, json={"error": "transient"}) if len(calls) == 1
                    else httpx.Response(200, json={"search_metadata": {"status": "Success"},
                                                   "organic_results": []}))
        collector = self.collector(GOOGLE_SOURCE_ID, handler)
        async def fake_sleep(delay):
            delays.append(delay)
        collector.sleep = fake_sleep
        result = await self.collect(collector, max_queries=1, max_requests=2,
                                    max_retries_per_page=1)
        self.assertEqual(result.attempted_calls, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(delays, [1.0])
        self.assertEqual(result.queries[0].reason, "END_OF_PAGES")

    async def test_transport_failure_retry_uses_a_new_charged_attempt(self):
        calls, delays = [], []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ConnectTimeout("synthetic timeout")
            return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                                             "organic_results": []})
        collector = self.collector(GOOGLE_SOURCE_ID, handler)
        async def fake_sleep(delay):
            delays.append(delay)
        collector.sleep = fake_sleep
        result = await self.collect(collector, max_queries=1, max_requests=2,
                                    max_retries_per_page=1)
        self.assertEqual(result.attempted_calls, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(delays, [1.0])
        self.assertEqual(result.queries[0].reason, "END_OF_PAGES")

    async def test_rate_limit_with_no_inline_retry_preserves_retry_time(self):
        def handler(_request):
            return httpx.Response(429, headers={"Retry-After": "9"}, json={"error": "rate limited"})
        result = await self.collect(self.collector(GOOGLE_SOURCE_ID, handler),
                                    max_queries=1, max_requests=1, max_retries_per_page=0)
        self.assertEqual(result.stop_reason, "DEFERRED")
        self.assertEqual(result.queries[0].retry_at, NOW + timedelta(seconds=9))

    async def test_long_retry_after_is_preserved_without_inline_retry(self):
        for index, source in enumerate((GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID)):
            if index:
                await self.asyncTearDown()
                await self.asyncSetUp()
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(429, headers={"Retry-After": "120"},
                                      json={"error": {"code": 429}})
            result = await self.collect(self.collector(source, handler),
                                        max_queries=1, max_requests=1, max_retries_per_page=1)
            self.assertEqual(result.stop_reason, "DEFERRED")
            self.assertEqual(result.queries[0].retry_at, NOW + timedelta(seconds=120))
            self.assertEqual(len(calls), 1)

    async def test_http_date_retry_after_is_preserved(self):
        retry_time = NOW + timedelta(seconds=120)
        def handler(_request):
            return httpx.Response(429, headers={"Retry-After": format_datetime(retry_time, usegmt=True)},
                                  json={"error": "rate limited"})
        result = await self.collect(self.collector(GOOGLE_SOURCE_ID, handler),
                                    max_queries=1, max_requests=1, max_retries_per_page=1)
        self.assertEqual(result.stop_reason, "DEFERRED")
        self.assertEqual(result.queries[0].retry_at, retry_time)

    async def test_all_registered_queries_can_complete_when_budget_allows(self):
        from app.jobs.public_admission import PoolLimit
        self.directory.cleanup()
        self.directory = tempfile.TemporaryDirectory()
        path = str(Path(self.directory.name) / "ledger.sqlite")
        config = policy(plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
                        pools={"serpapi": PoolLimit(14, 14, 14, 14, 1),
                               "youtube": PoolLimit(2, 5, 2, 5, 1)})
        self.control = coordinator(path, config)
        self.job = self.control.admit("public-fetch-idempotency", now=NOW)["job_id"]
        self.fence = self.control.claim(self.job, "worker", now=NOW)["fence"]
        self.runner = GuardedAttemptRunner(self.control, clock=lambda: NOW)
        def handler(_request):
            return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                "organic_results": [{"position": 1, "link": "https://example.org/item",
                                     "title": "Fixture"}]})
        result = await self.collect(self.collector(GOOGLE_SOURCE_ID, handler),
                                    max_queries=14, max_requests=14)
        self.assertEqual(result.status, "SUCCESS")
        self.assertTrue(result.complete)
        self.assertEqual(result.attempted_calls, 14)
        self.assertEqual(len(result.records[0].evidence), 14)
