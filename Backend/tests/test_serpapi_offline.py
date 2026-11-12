"""Synthetic contracts for the two SerpApi engines; never contacts a provider."""

import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from app.sources.bing_search.adapter import BingSearchAdapter
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID
from app.sources.google_search.adapter import GoogleSearchAdapter
from app.sources.http_policy import HttpPolicy
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.serpapi.adapter import ExecutionLimits
from app.sources.serpapi.client import SerpApiClient, build_request_params, create_offline_client
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.models import ScopedOffset
from app.sources.serpapi.normalization import canonical_url
from app.sources.serpapi.pagination import build_contexts, initial_offset, next_offset
from app.sources.serpapi.quota import SharedQuotaPool

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
FAKE_KEY = "synthetic-key-never-real"


class Resolver:
    def resolve(self, reference):
        assert reference == "SERPAPI_API_KEY"
        return FAKE_KEY


class Contracts(unittest.TestCase):
    def test_fixed_requests_and_plan_are_validated(self):
        google = build_contexts(DEFAULT_QUERY_PLAN, GOOGLE_SOURCE_ID, NOW)[0]
        bing = build_contexts(DEFAULT_QUERY_PLAN, BING_SOURCE_ID, NOW)[0]
        self.assertEqual(build_request_params(google, initial_offset(google)), {
            "engine": "google", "q": google.canonical_query, "gl": "us", "hl": "en",
            "google_domain": "google.com", "start": 0, "output": "json",
        })
        self.assertEqual(build_request_params(bing, initial_offset(bing)), {
            "engine": "bing", "q": bing.canonical_query, "mkt": "en-US", "first": 1,
            "output": "json",
        })
        with self.assertRaises(SerpApiError):
            build_request_params(replace(google, canonical_query="arbitrary"), initial_offset(google))
        with self.assertRaises(SerpApiError):
            build_request_params(google, ScopedOffset(3, google.query_fingerprint))
        with self.assertRaises(SerpApiError):
            build_request_params(bing, ScopedOffset(1, google.query_fingerprint))

    def test_bing_next_offset_is_variable_and_link_is_never_trusted(self):
        ctx = build_contexts(DEFAULT_QUERY_PLAN, BING_SOURCE_ID, NOW)[0]
        link = "https://serpapi.com/search.json?" + urlencode({
            "engine": "bing", "q": ctx.canonical_query, "first": "6",
        })
        self.assertEqual(next_offset(ctx, initial_offset(ctx), link).value, 6)
        for bad in (link.replace("serpapi.com", "example.invalid"),
                    link.replace("first=6", "first=1"), link.replace("engine=bing", "engine=google")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                next_offset(ctx, initial_offset(ctx), bad)

    def test_canonical_url_removes_tracking_and_rejects_credentials(self):
        self.assertEqual(canonical_url("https://EXAMPLE.com/a?utm_source=x&id=3#fragment"),
                         "https://example.com/a?id=3")
        for value in ("javascript:alert(1)", "https://u:p@example.com/", "https://x/?api_key=secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_url(value)

    def test_live_transport_refused_and_quota_shared(self):
        with self.assertRaises(TypeError):
            create_offline_client(httpx.AsyncHTTPTransport())
        pool = SharedQuotaPool(3, 2)
        self.assertTrue(pool.reserve())
        self.assertTrue(pool.reserve())
        self.assertFalse(pool.reserve())
        self.assertEqual(pool.snapshot().attempted_requests, 2)

    def test_shared_pool_reservations_are_atomic_between_threads(self):
        pool = SharedQuotaPool(5, 5)
        with ThreadPoolExecutor(max_workers=10) as executor:
            reservations = list(executor.map(lambda _: pool.reserve(), range(30)))
        self.assertEqual(sum(reservations), 5)
        self.assertEqual(pool.snapshot().reserved_cost_units, 5)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_engines_dedup_cross_query_and_preserve_evidence(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                "organic_results": [{"position": 1, "link": "https://example.com/a?utm_x=1",
                                     "title": "Example"}]})

        pool = SharedQuotaPool(28, 28)
        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), pool)
            for cls in (GoogleSearchAdapter, BingSearchAdapter):
                result = await cls(client, utcnow=lambda: NOW).execute()
                self.assertEqual(result.status, "SUCCESS")
                self.assertEqual(len(result.records), 1)
                self.assertEqual(len(result.records[0].evidence), 14)
                self.assertEqual(result.records[0].data_mode, "SYNTHETIC")
                self.assertNotIn(FAKE_KEY, repr(result))
        self.assertEqual(len(seen), 28)
        self.assertTrue(all(r.url.host == "serpapi.com" for r in seen))
        self.assertEqual(pool.snapshot().attempted_requests, 28)

    async def test_pagination_page_cap_and_cache_provenance(self):
        def handler(request):
            q = request.url.params["q"]
            link = "https://serpapi.com/search.json?" + urlencode({
                "engine": "bing", "q": q, "first": "6",
            })
            return httpx.Response(200, json={"search_metadata": {"status": "Cached",
                "created_at": "2026-11-10 12:00:00 UTC"},
                "organic_results": [{"position": 1, "link": "https://example.com/"}],
                "serpapi_pagination": {"next": link}})

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(2, 2))
            result = await BingSearchAdapter(client, utcnow=lambda: NOW).execute(
                limits=ExecutionLimits(max_queries=1))
        self.assertEqual(result.queries[0].stop_reason, "PAGE_CAP_REACHED")
        self.assertFalse(result.complete)
        self.assertEqual(result.records[0].evidence[0].cache_state, "CACHED")
        self.assertEqual(result.records[0].evidence[0].provider_created_at,
                         datetime(2026, 11, 10, 12, tzinfo=timezone.utc))

    async def test_quota_error_and_redaction(self):
        def handler(request):
            return httpx.Response(429, json={"error": "Your account has run out of searches. " + FAKE_KEY})

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(2, 2))
            result = await GoogleSearchAdapter(client, utcnow=lambda: NOW).execute()
        self.assertEqual(result.stop_reason, "QUOTA_EXHAUSTED")
        self.assertEqual(result.quota.attempted_requests, 1)
        self.assertNotIn(FAKE_KEY, repr(result))

    async def test_retry_budget_and_pre_cancel(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(503, json={"error": "unavailable"})

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(1, 1),
                                   HttpPolicy(max_retries=1, max_retry_wait_seconds=0))
            result = await GoogleSearchAdapter(client, utcnow=lambda: NOW).execute()
            self.assertEqual(result.stop_reason, "REQUEST_BUDGET_EXHAUSTED")
            event = asyncio.Event()
            event.set()
            cancelled = await BingSearchAdapter(client, utcnow=lambda: NOW).execute(
                cancellation_event=event)
        self.assertEqual(cancelled.stop_reason, "CANCELLED")
        self.assertEqual(len(calls), 1)

    async def test_multi_page_bing_preserves_offsets_and_detects_repeated_pages(self):
        offsets = []

        def handler(request):
            offset = int(request.url.params["first"])
            offsets.append(offset)
            q = request.url.params["q"]
            data = {"search_metadata": {"status": "Success"},
                    "organic_results": [{"position": 1, "link": "https://example.com/"}]}
            if offset == 1:
                data["serpapi_pagination"] = {"next": "https://serpapi.com/search.json?" +
                    urlencode({"engine": "bing", "q": q, "first": 6})}
            return httpx.Response(200, json=data)

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(4, 4))
            result = await BingSearchAdapter(client, utcnow=lambda: NOW).execute(
                limits=ExecutionLimits(max_queries=1, max_pages_per_query=2))
        self.assertEqual(offsets, [1, 6])
        self.assertEqual(result.queries[0].stop_reason, "REPEATED_PAGE")
        self.assertFalse(result.complete)

    async def test_empty_response_and_malformed_response_are_distinct(self):
        payloads = [
            {"search_metadata": {"status": "Success"},
             "search_information": {"organic_results_state": "Fully empty"},
             "error": "No results"},
            {"search_metadata": {"status": "Success"}},
            {"search_metadata": {"status": []}, "organic_results": []},
        ]

        def handler(request):
            return httpx.Response(200, json=payloads.pop(0))

        context = build_contexts(DEFAULT_QUERY_PLAN, GOOGLE_SOURCE_ID, NOW)[0]
        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(3, 3))
            page = await client.search(context, initial_offset(context))
            self.assertEqual(page.items, ())
            for code in ("MALFORMED_RESPONSE", "PROVIDER_ERROR"):
                with self.assertRaises(SerpApiError) as caught:
                    await client.search(context, initial_offset(context))
                self.assertEqual(caught.exception.code, code)

    async def test_provider_errors_are_classified_without_echoing_secret(self):
        responses = [
            httpx.Response(401, json={"error": FAKE_KEY}),
            httpx.Response(403, json={"error": FAKE_KEY}),
            httpx.Response(429, headers={"Retry-After": "9999"}, json={"error": FAKE_KEY}),
            httpx.Response(200, json={"search_metadata": {"status": "Success"},
                                     "organic_results": [{"link": "https://example.com/",
                                                          "title": FAKE_KEY}]}),
        ]

        def handler(request):
            return responses.pop(0)

        context = build_contexts(DEFAULT_QUERY_PLAN, GOOGLE_SOURCE_ID, NOW)[0]
        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(4, 4))
            for code in ("UNAUTHORIZED", "PERMISSION_DENIED", "RATE_LIMITED", "MALFORMED_RESPONSE"):
                with self.assertRaises(SerpApiError) as caught:
                    await client.search(context, initial_offset(context))
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(FAKE_KEY, repr(caught.exception))
                if code == "RATE_LIMITED":
                    self.assertFalse(caught.exception.retryable)

    async def test_cancellation_interrupts_in_flight_request(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handler(request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(2, 2))
            event = asyncio.Event()
            task = asyncio.create_task(GoogleSearchAdapter(client, utcnow=lambda: NOW).execute(
                cancellation_event=event))
            await asyncio.wait_for(started.wait(), 1)
            event.set()
            result = await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result.stop_reason, "CANCELLED")

    async def test_http_timeout_retry_respects_shared_budget(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(408, json={"error": "timeout"})
            return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                "organic_results": [{"link": "https://example.com/"}]})

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(2, 2),
                                   HttpPolicy(max_retries=1, max_retry_wait_seconds=0))
            result = await GoogleSearchAdapter(client, utcnow=lambda: NOW).execute(
                limits=ExecutionLimits(max_queries=1))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.quota.attempted_requests, 2)
        self.assertEqual(len(result.records), 1)

    async def test_google_two_distinct_pages_keep_offsets_and_evidence(self):
        offsets = []

        def handler(request):
            offset = int(request.url.params["start"])
            offsets.append(offset)
            data = {"search_metadata": {"status": "Success"}, "organic_results": [
                {"position": 1, "link": "https://example.com/", "title": str(offset)}]}
            if offset == 0:
                data["serpapi_pagination"] = {"next": "https://serpapi.com/search.json?" +
                    urlencode({"engine": "google", "q": request.url.params["q"], "start": 10})}
            return httpx.Response(200, json=data)

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(2, 2))
            result = await GoogleSearchAdapter(client, utcnow=lambda: NOW).execute(
                limits=ExecutionLimits(max_queries=1, max_pages_per_query=2))
        self.assertEqual(offsets, [0, 10])
        self.assertTrue(result.queries[0].complete)
        self.assertEqual([e.offset for e in result.records[0].evidence], [0, 10])
        self.assertFalse(result.complete)  # Only one of the fourteen queries was selected.

    async def test_bing_two_distinct_pages_reuse_only_validated_offset(self):
        offsets = []

        def handler(request):
            offset = int(request.url.params["first"])
            offsets.append(offset)
            data = {"search_metadata": {"status": "Success"}, "organic_results": [
                {"position": 1, "link": "https://example.com/", "title": str(offset)}]}
            if offset == 1:
                data["serpapi_pagination"] = {"next": "https://serpapi.com/search.json?" +
                    urlencode({"engine": "bing", "q": request.url.params["q"], "first": 6,
                               "api_key": "untrusted-provider-value"})}
            return httpx.Response(200, json=data)

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(2, 2))
            result = await BingSearchAdapter(client, utcnow=lambda: NOW).execute(
                limits=ExecutionLimits(max_queries=1, max_pages_per_query=2))
        self.assertEqual(offsets, [1, 6])
        self.assertTrue(result.queries[0].complete)
        self.assertEqual([e.offset for e in result.records[0].evidence], [1, 6])
        self.assertNotIn("untrusted-provider-value", repr(result))

    async def test_missing_credential_does_not_reserve_or_send_request(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(500)

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, None, SharedQuotaPool(1, 1))
            result = await GoogleSearchAdapter(client, utcnow=lambda: NOW).execute()
        self.assertEqual(result.stop_reason, "CREDENTIAL_NOT_CONFIGURED")
        self.assertEqual(result.quota.attempted_requests, 0)
        self.assertFalse(calls)

    async def test_future_provider_timestamp_is_rejected_with_quality_flag(self):
        def handler(request):
            return httpx.Response(200, json={"search_metadata": {"status": "Cached",
                "created_at": "2026-11-12 12:00:00 UTC"},
                "organic_results": [{"link": "https://example.com/"}]})

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(1, 1))
            result = await GoogleSearchAdapter(client, utcnow=lambda: NOW).execute(
                limits=ExecutionLimits(max_queries=1))
        self.assertIsNone(result.records[0].evidence[0].provider_created_at)
        self.assertIn("PROVIDER_CREATION_TIME_IN_FUTURE", result.records[0].quality_flags)

    async def test_malformed_large_and_excess_results_are_bounded(self):
        responses = [
            httpx.Response(200, content=b"{", headers={"content-type": "application/json"}),
            httpx.Response(200, content=b"x" * 1_000_001,
                           headers={"content-type": "application/json"}),
            httpx.Response(200, json={"search_metadata": {"status": "Success"},
                                      "organic_results": [{}] * 101}),
        ]

        def handler(request):
            return responses.pop(0)

        context = build_contexts(DEFAULT_QUERY_PLAN, GOOGLE_SOURCE_ID, NOW)[0]
        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(3, 3))
            for code in ("MALFORMED_RESPONSE", "RESPONSE_TOO_LARGE", "PROVIDER_RESULT_LIMIT"):
                with self.assertRaises(SerpApiError) as caught:
                    await client.search(context, initial_offset(context))
                self.assertEqual(caught.exception.code, code)

    async def test_deadline_stops_hanging_transport(self):
        async def handler(request):
            await asyncio.Event().wait()

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(2, 2), HttpPolicy(max_retries=0))
            result = await GoogleSearchAdapter(client, utcnow=lambda: NOW).execute(
                limits=ExecutionLimits(max_queries=1, deadline_seconds=1.0))
        self.assertEqual(result.stop_reason, "DEADLINE_EXCEEDED")
        self.assertFalse(result.complete)

    async def test_transport_exception_does_not_expose_request_url(self):
        def handler(request):
            raise httpx.ConnectError(str(request.url), request=request)

        context = build_contexts(DEFAULT_QUERY_PLAN, BING_SOURCE_ID, NOW)[0]
        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(1, 1))
            with self.assertRaises(SerpApiError) as caught:
                await client.search(context, initial_offset(context))
        self.assertEqual(caught.exception.code, "NETWORK_ERROR")
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertNotIn(FAKE_KEY, repr(caught.exception))

    async def test_invalid_next_link_retains_valid_page_and_reports_rejections(self):
        def handler(request):
            return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                "organic_results": [{"link": "https://example.com/"}, {"link": "javascript:x"}],
                "serpapi_pagination": {"next": "https://attacker.invalid/"}})

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(1, 1))
            result = await BingSearchAdapter(client, utcnow=lambda: NOW).execute(
                limits=ExecutionLimits(max_queries=1, max_pages_per_query=2))
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.queries[0].stop_reason, "INVALID_PAGE_OFFSET")
        self.assertEqual(result.queries[0].rejection_reasons, ("INVALID_RESULT",))
        self.assertIn("PROVIDER_CREATION_TIME_UNAVAILABLE", result.records[0].quality_flags)

    async def test_rate_limit_retry_after_reserves_another_attempt(self):
        calls, delays = [], []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "2"},
                                      json={"error": "throughput limit"})
            return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                "organic_results": [{"link": "https://example.com/"}]})

        async def fake_sleep(seconds):
            delays.append(seconds)

        async with create_offline_client(httpx.MockTransport(handler)) as http:
            client = SerpApiClient(http, Resolver(), SharedQuotaPool(2, 2),
                                   HttpPolicy(max_retries=1, max_retry_wait_seconds=3))
            result = await GoogleSearchAdapter(client, utcnow=lambda: NOW, sleep=fake_sleep).execute(
                limits=ExecutionLimits(max_queries=1))
        self.assertEqual(delays, [2.0])
        self.assertEqual(result.quota.attempted_requests, 2)
        self.assertEqual(len(result.records), 1)


if __name__ == "__main__":
    unittest.main()
