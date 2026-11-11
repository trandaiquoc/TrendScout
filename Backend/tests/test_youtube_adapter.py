"""Offline-only contract tests for the query-based YouTube source adapter."""

import asyncio
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.sources.http_policy import HttpPolicy
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.youtube import YouTubeSearchAdapter
from app.sources.youtube.client import (
    YOUTUBE_API_KEY_HEADER,
    YOUTUBE_SEARCH_ENDPOINT,
    create_offline_youtube_client,
)
from app.sources.youtube.errors import YouTubeErrorCode, YouTubeSourceError
from app.sources.youtube.models import ScopedPageToken
from app.sources.youtube.pagination import ExecutionLimits, build_execution_plan

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"
SYNTHETIC_PAGE = json.loads((FIXTURES / "synthetic_search_page.json").read_text())
QUOTA_ERROR = json.loads((FIXTURES / "synthetic_quota_exceeded.json").read_text())
FAKE_API_KEY = "offline-test-key-never-real"
FIXED_NOW = datetime(2026, 11, 11, 12, 34, 56, tzinfo=timezone.utc)


class FakeCredentialResolver:
    def __init__(self, value=FAKE_API_KEY):
        self.value = value
        self.requested_references = []

    def resolve(self, reference):
        self.requested_references.append(reference)
        return self.value


class FakeClock:
    def __init__(self, now=FIXED_NOW):
        self.now = now
        self.monotonic_now = 0.0
        self.sleeps = []

    def utcnow(self):
        return self.now

    def monotonic(self):
        return self.monotonic_now

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.monotonic_now += delay


def response(request, payload, *, status_code=200, headers=None):
    response_headers = {"content-type": "application/json; charset=utf-8"}
    response_headers.update(headers or {})
    return httpx.Response(
        status_code,
        headers=response_headers,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        request=request,
    )


class YouTubeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def run_adapter(
        self,
        handler,
        *,
        plan=None,
        limits=None,
        policy=None,
        clock=None,
        credential=FAKE_API_KEY,
        cancellation_event=None,
        max_response_bytes=1_000_000,
    ):
        transport = httpx.MockTransport(handler)
        client = create_offline_youtube_client(transport)
        fake_clock = clock or FakeClock()
        resolver = FakeCredentialResolver(credential)
        selected_plan = plan or DEFAULT_QUERY_PLAN
        if limits is None:
            limits = {"max_queries": len(selected_plan.seeds) if plan is not None else 1}
        elif isinstance(limits, dict):
            limits = {**limits, "max_queries": limits.get("max_queries", 1)}
        adapter = YouTubeSearchAdapter(
            client=client,
            credential_resolver=resolver,
            http_policy=policy or HttpPolicy(max_retries=0, max_retry_wait_seconds=0),
            utcnow=fake_clock.utcnow,
            monotonic=fake_clock.monotonic,
            sleep=fake_clock.sleep,
            max_response_bytes=max_response_bytes,
        )
        try:
            result = await adapter.execute(
                plan=selected_plan,
                limits=ExecutionLimits(**limits) if isinstance(limits, dict) else limits,
                cancellation_event=cancellation_event,
            )
        finally:
            await client.aclose()
        return result, resolver, fake_clock

    async def test_full_default_plan_builds_fixed_requests_and_preserves_query_evidence(self):
        requests = []

        def handler(request):
            requests.append(request)
            return response(request, {**SYNTHETIC_PAGE, "items": [SYNTHETIC_PAGE["items"][0]]})

        result, resolver, _ = await self.run_adapter(handler, plan=DEFAULT_QUERY_PLAN)

        self.assertEqual(len(requests), 14)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.data_mode, "SYNTHETIC")
        self.assertEqual(result.budget.planned_calls, 14)
        self.assertEqual(result.budget.attempted_calls, 14)
        self.assertEqual(result.budget.completed_calls, 14)
        self.assertEqual(result.budget.failed_calls, 0)
        self.assertEqual(result.budget.pages_fetched, 14)
        self.assertEqual(result.budget.remaining_budget, 0)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.records[0].query_evidence), 14)
        self.assertEqual(resolver.requested_references, ["YOUTUBE_API_KEY"])
        self.assertEqual({request.url.host for request in requests}, {"www.googleapis.com"})
        self.assertEqual({request.url.path for request in requests}, {"/youtube/v3/search"})
        self.assertEqual({request.url.scheme for request in requests}, {"https"})
        self.assertTrue(
            all(str(request.url).startswith(YOUTUBE_SEARCH_ENDPOINT) for request in requests)
        )
        query_params = [dict(request.url.params) for request in requests]
        self.assertEqual({params["part"] for params in query_params}, {"snippet"})
        self.assertEqual({params["type"] for params in query_params}, {"video"})
        self.assertEqual({params["regionCode"] for params in query_params}, {"US"})
        self.assertEqual({params["relevanceLanguage"] for params in query_params}, {"en"})
        self.assertEqual({params["order"] for params in query_params}, {"relevance"})
        self.assertEqual({params["maxResults"] for params in query_params}, {"50"})
        self.assertEqual(
            {params["publishedAfter"] for params in query_params}, {"2026-11-04T12:34:56Z"}
        )
        self.assertNotIn("key", query_params[0])
        self.assertTrue(
            all(request.headers[YOUTUBE_API_KEY_HEADER] == FAKE_API_KEY for request in requests)
        )
        self.assertNotIn("pageToken", query_params[0])
        self.assertEqual(
            {params["q"] for params in query_params},
            {seed.query for seed in DEFAULT_QUERY_PLAN.seeds},
        )
        self.assertNotIn(FAKE_API_KEY, repr(result))
        for record in result.records:
            self.assertEqual(record.query_plan_id, "demo-us-en")
            self.assertEqual(record.query_plan_version, 1)
            self.assertEqual(record.record_schema_version, 1)
            self.assertEqual(record.query_fingerprint, record.query_evidence[0].query_fingerprint)
            self.assertEqual(record.market_filter, "US")
            self.assertEqual(record.language_filter, "en")
            self.assertEqual(record.metric.name, "result_rank")
            self.assertNotIn("view_count", record.__dataclass_fields__)

    async def test_missing_credentials_fail_closed_without_network_request(self):
        calls = []

        def handler(request):
            calls.append(request)
            return response(request, SYNTHETIC_PAGE)

        result, _, _ = await self.run_adapter(handler, credential=None)

        self.assertEqual(calls, [])
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.budget.attempted_calls, 0)
        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.CREDENTIAL_NOT_CONFIGURED)

    async def test_page_cap_reports_more_available_without_claiming_completeness(self):
        requests = []

        def handler(request):
            requests.append(request)
            return response(request, {**SYNTHETIC_PAGE, "nextPageToken": "cursorA"})

        result, _, _ = await self.run_adapter(handler, plan=DEFAULT_QUERY_PLAN)

        query = result.queries[0]
        self.assertEqual(len(requests), 14)
        self.assertEqual(query.pagination_state, "MORE_AVAILABLE")
        self.assertEqual(query.stop_reason, "PAGE_CAP_REACHED")
        self.assertFalse(result.complete)
        self.assertEqual(result.status, "SUCCESS")

    async def test_two_page_execution_uses_provider_token_and_keeps_both_evidence_links(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.params.get("pageToken") is None:
                return response(
                    request,
                    {
                        **SYNTHETIC_PAGE,
                        "items": [SYNTHETIC_PAGE["items"][0]],
                        "nextPageToken": "cursor-A",
                    },
                )
            return response(
                request,
                {
                    **SYNTHETIC_PAGE,
                    "items": [SYNTHETIC_PAGE["items"][0]],
                },
            )

        result, _, _ = await self.run_adapter(
            handler, limits={"max_pages_per_query": 2, "max_requests": 2}
        )

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].url.params["pageToken"], "cursor-A")
        self.assertEqual(result.budget.pages_fetched, 2)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(len(result.records[0].query_evidence), 2)
        self.assertEqual(
            [evidence.page_index for evidence in result.records[0].query_evidence], [1, 2]
        )

    async def test_repeated_page_token_stops_without_looping(self):
        requests = []

        def handler(request):
            requests.append(request)
            return response(
                request,
                {
                    **SYNTHETIC_PAGE,
                    "nextPageToken": "cursor-A",
                },
            )

        result, _, _ = await self.run_adapter(
            handler, limits={"max_pages_per_query": 5, "max_requests": 5}
        )

        self.assertEqual(len(requests), 2)
        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.INVALID_PAGE_TOKEN)
        self.assertEqual(result.queries[0].stop_reason, "REPEATED_PAGE_TOKEN")

    async def test_cursor_cannot_be_reused_with_another_query_context(self):
        execution = build_execution_plan(DEFAULT_QUERY_PLAN, FIXED_NOW)
        token = ScopedPageToken("cursor-A", execution.queries[0].context_key)

        with self.assertRaises(YouTubeSourceError) as raised:
            await self._search_with_context(execution.queries[1], token)

        self.assertEqual(raised.exception.code, YouTubeErrorCode.INVALID_PAGE_TOKEN)

    async def test_cursor_cannot_be_reused_with_another_publication_cutoff(self):
        first = build_execution_plan(DEFAULT_QUERY_PLAN, FIXED_NOW)
        later = build_execution_plan(DEFAULT_QUERY_PLAN, FIXED_NOW + timedelta(days=1))
        token = ScopedPageToken("cursor-A", first.queries[0].context_key)

        with self.assertRaises(YouTubeSourceError) as raised:
            await self._search_with_context(later.queries[0], token)

        self.assertEqual(raised.exception.code, YouTubeErrorCode.INVALID_PAGE_TOKEN)

    def test_execution_rejects_modified_unregistered_query_plan(self):
        altered = replace(DEFAULT_QUERY_PLAN, seeds=DEFAULT_QUERY_PLAN.seeds[:-1])

        with self.assertRaisesRegex(ValueError, "registered server-controlled query plan"):
            build_execution_plan(altered, FIXED_NOW)

    async def _search_with_context(self, context, token):
        client = create_offline_youtube_client(
            httpx.MockTransport(lambda request: response(request, SYNTHETIC_PAGE))
        )
        from app.sources.youtube.client import YouTubeSearchClient

        try:
            search_client = YouTubeSearchClient(
                client=client,
                credential_resolver=FakeCredentialResolver(),
                http_policy=HttpPolicy(max_retries=0),
            )
            return await search_client.search(context, token, api_key=FAKE_API_KEY, max_results=50)
        finally:
            await client.aclose()

    async def test_provider_invalid_page_token_is_classified_without_raw_body(self):
        def handler(request):
            return response(
                request,
                {"error": {"code": 400, "errors": [{"reason": "invalidPageToken"}]}},
                status_code=400,
            )

        result, _, _ = await self.run_adapter(
            handler, limits={"max_pages_per_query": 2, "max_requests": 2}
        )

        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.INVALID_PAGE_TOKEN)
        self.assertNotIn(FAKE_API_KEY, repr(result))
        self.assertNotIn("invalidPageToken", result.queries[0].error.safe_message)

    async def test_empty_page_can_continue_with_next_token(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.params.get("pageToken") is None:
                return response(
                    request, {**SYNTHETIC_PAGE, "items": [], "nextPageToken": "cursor-A"}
                )
            return response(request, {**SYNTHETIC_PAGE, "items": [SYNTHETIC_PAGE["items"][1]]})

        result, _, _ = await self.run_adapter(
            handler, limits={"max_pages_per_query": 2, "max_requests": 2}
        )

        self.assertEqual(len(requests), 2)
        self.assertEqual(result.queries[0].returned_count, 1)
        self.assertEqual(result.records[0].query_evidence[0].result_rank, 1)

    async def test_request_budget_is_reserved_before_attempt_and_stops_remaining_queries(self):
        requests = []

        def handler(request):
            requests.append(request)
            return response(request, SYNTHETIC_PAGE)

        result, _, _ = await self.run_adapter(
            handler,
            plan=DEFAULT_QUERY_PLAN,
            limits={"max_requests": 1, "max_queries": 14},
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.budget.attempted_calls, 1)
        self.assertEqual(result.budget.completed_calls, 1)
        self.assertEqual(result.budget.remaining_budget, 0)
        self.assertEqual(result.stop_reason, "REQUEST_BUDGET_EXHAUSTED")
        self.assertEqual(len(result.queries), 14)
        self.assertEqual(sum(query.requested_pages for query in result.queries), 1)

    async def test_retries_consume_budget_and_use_shared_bounded_backoff(self):
        calls = []
        clock = FakeClock()

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return response(request, {"error": {"code": 503}}, status_code=503)
            return response(request, SYNTHETIC_PAGE)

        result, _, clock = await self.run_adapter(
            handler,
            policy=HttpPolicy(max_retries=1, max_retry_wait_seconds=2),
            clock=clock,
            limits={"max_requests": 2},
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(result.budget.attempted_calls, 2)
        self.assertEqual(result.budget.completed_calls, 1)
        self.assertEqual(result.budget.failed_calls, 1)
        self.assertEqual(result.budget.retried_calls, 1)
        self.assertEqual(clock.sleeps, [1.0])

    async def test_rate_limit_honors_bounded_retry_after(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return response(
                    request,
                    {"error": {"code": 429}},
                    status_code=429,
                    headers={"Retry-After": "2"},
                )
            return response(request, SYNTHETIC_PAGE)

        result, _, clock = await self.run_adapter(
            handler,
            policy=HttpPolicy(max_retries=1, max_retry_wait_seconds=5),
            limits={"max_requests": 2},
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(clock.sleeps, [2.0])
        self.assertEqual(result.budget.retried_calls, 1)

    async def test_rate_limit_without_retry_after_uses_bounded_backoff(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return response(request, {"error": {"code": 429}}, status_code=429)
            return response(request, SYNTHETIC_PAGE)

        result, _, clock = await self.run_adapter(
            handler,
            policy=HttpPolicy(max_retries=1, max_retry_wait_seconds=5),
            limits={"max_requests": 2},
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(clock.sleeps, [1.0])
        self.assertEqual(result.budget.retried_calls, 1)

    async def test_rate_limit_above_retry_wait_bound_stops_without_retry(self):
        calls = []

        def handler(request):
            calls.append(request)
            return response(
                request,
                {"error": {"code": 429}},
                status_code=429,
                headers={"Retry-After": "120"},
            )

        result, _, _ = await self.run_adapter(
            handler,
            policy=HttpPolicy(max_retries=3, max_retry_wait_seconds=5),
            limits={"max_requests": 4},
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.RATE_LIMITED)
        self.assertTrue(result.queries[0].error.retry_after_exceeds_limit)

    async def test_retry_is_refused_when_request_budget_has_no_reserved_attempt(self):
        calls = []

        def handler(request):
            calls.append(request)
            return response(request, {"error": {"code": 503}}, status_code=503)

        result, _, _ = await self.run_adapter(
            handler,
            policy=HttpPolicy(max_retries=2, max_retry_wait_seconds=0),
            limits={"max_requests": 1},
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result.budget.attempted_calls, 1)
        self.assertEqual(result.budget.retried_calls, 0)
        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.RETRY_BUDGET_EXHAUSTED)

    async def test_quota_exhaustion_stops_all_remaining_queries(self):
        calls = []

        def handler(request):
            calls.append(request)
            return response(request, QUOTA_ERROR, status_code=403)

        result, _, _ = await self.run_adapter(handler, plan=DEFAULT_QUERY_PLAN)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.stop_reason, "QUOTA_EXHAUSTED")
        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.QUOTA_EXHAUSTED)
        self.assertEqual(result.budget.failed_calls, 1)
        self.assertEqual(result.budget.attempted_calls, 1)

    async def test_cancellation_stops_before_next_page(self):
        requests = []
        cancellation = asyncio.Event()

        def handler(request):
            requests.append(request)
            cancellation.set()
            return response(request, {**SYNTHETIC_PAGE, "nextPageToken": "cursor-A"})

        result, _, _ = await self.run_adapter(
            handler,
            limits={"max_pages_per_query": 2, "max_requests": 28},
            cancellation_event=cancellation,
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.stop_reason, "CANCELLED")

    async def test_absolute_batch_deadline_stops_before_next_page(self):
        requests = []
        clock = FakeClock()

        def handler(request):
            requests.append(request)
            clock.monotonic_now = 2.0
            return response(request, {**SYNTHETIC_PAGE, "nextPageToken": "cursor-A"})

        result, _, _ = await self.run_adapter(
            handler,
            clock=clock,
            limits={"max_pages_per_query": 2, "max_requests": 28, "deadline_seconds": 1},
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.stop_reason, "DEADLINE_EXCEEDED")

    async def test_malformed_optional_fields_are_rejected_or_safely_omitted(self):
        def handler(request):
            payload = {
                "kind": "youtube#searchListResponse",
                "items": [
                    {
                        "kind": "youtube#searchResult",
                        "id": {"kind": "youtube#video", "videoId": "AbCdEf12345"},
                        "snippet": {
                            "title": chr(0xD800),
                            "publishedAt": "2026-11-08T12:00:00Z",
                        },
                    },
                    {
                        "kind": "youtube#searchResult",
                        "id": {"kind": "youtube#video", "videoId": "ZyXwVu98765"},
                        "snippet": {
                            "title": "Odd optional fields",
                            "publishedAt": "2026-11-08T12:00:00Z",
                            "liveBroadcastContent": [],
                            "thumbnails": {"default": {"url": "https://i.ytimg.com:[bad"}},
                        },
                    },
                ],
            }
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
                request=request,
            )

        result, _, _ = await self.run_adapter(handler)

        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.queries[0].rejection_reasons, ("INVALID_TEXT_FIELD",))
        record = result.records[0]
        self.assertIsNone(record.live_broadcast_content)
        self.assertIn("UNKNOWN_LIVE_BROADCAST_CONTENT", record.quality_flags)
        self.assertEqual(record.thumbnail_urls, ())
        self.assertIn("INVALID_THUMBNAIL_URL_OMITTED", record.quality_flags)

    async def test_invalid_provider_next_page_token_is_rejected(self):
        def handler(request):
            return response(request, {**SYNTHETIC_PAGE, "nextPageToken": {"cursor": "bad"}})

        result, _, _ = await self.run_adapter(handler)

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.INVALID_PAGE_TOKEN)

    async def test_missing_video_id_is_rejected_with_a_stable_reason(self):
        def handler(request):
            return response(
                request,
                {
                    "kind": "youtube#searchListResponse",
                    "items": [
                        {
                            "kind": "youtube#searchResult",
                            "id": {"kind": "youtube#video"},
                            "snippet": {"title": "No video ID"},
                        }
                    ],
                },
            )

        result, _, _ = await self.run_adapter(handler)

        self.assertEqual(result.records, ())
        self.assertEqual(result.queries[0].rejected_count, 1)
        self.assertEqual(
            result.queries[0].rejection_reasons,
            ("MISSING_OR_INVALID_VIDEO_ID",),
        )

    async def test_oversized_provider_response_is_rejected_before_parsing(self):
        def handler(request):
            return response(
                request, {"kind": "youtube#searchListResponse", "items": [], "padding": "x" * 2048}
            )

        result, _, _ = await self.run_adapter(handler, max_response_bytes=1024)

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.RESPONSE_TOO_LARGE)

    async def test_live_transport_is_rejected_without_sending_a_request(self):
        class NonMockTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise AssertionError("non-mock transport must not be used")

        client = httpx.AsyncClient(transport=NonMockTransport())
        try:
            with self.assertRaisesRegex(ValueError, "refuses live HTTP transports"):
                YouTubeSearchAdapter(
                    client=client,
                    credential_resolver=FakeCredentialResolver(),
                    http_policy=HttpPolicy(max_retries=0),
                )
        finally:
            await client.aclose()

    async def test_redirect_is_not_followed_to_another_host(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                302,
                headers={"Location": "https://attacker.invalid/collect"},
                request=request,
            )

        result, _, _ = await self.run_adapter(handler)

        self.assertEqual(len(requests), 1)
        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.UNEXPECTED_REDIRECT)

    async def test_transport_exception_is_redacted_from_result(self):
        def handler(request):
            raise httpx.ConnectError(
                f"failed for credential {request.headers[YOUTUBE_API_KEY_HEADER]}",
                request=request,
            )

        result, _, _ = await self.run_adapter(handler)

        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.NETWORK_ERROR)
        self.assertNotIn(FAKE_API_KEY, repr(result))
        self.assertNotIn(FAKE_API_KEY, str(result.queries[0].error))

    async def test_upstream_timeout_and_async_cancellation_are_preserved(self):
        started = asyncio.Event()

        async def hanging_handler(request):
            started.set()
            await asyncio.Event().wait()

        timed_out, _, _ = await self.run_adapter(
            hanging_handler,
            policy=HttpPolicy(total_timeout_seconds=0.01, max_retries=0),
            limits={"deadline_seconds": 2},
        )
        self.assertEqual(timed_out.queries[0].error.code, YouTubeErrorCode.UPSTREAM_TIMEOUT)

        started.clear()
        client = create_offline_youtube_client(httpx.MockTransport(hanging_handler))
        adapter = YouTubeSearchAdapter(
            client=client,
            credential_resolver=FakeCredentialResolver(),
            http_policy=HttpPolicy(total_timeout_seconds=5, max_retries=0),
        )
        task = asyncio.create_task(
            adapter.execute(limits=ExecutionLimits(max_queries=1, deadline_seconds=10))
        )
        await started.wait()
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            await client.aclose()

    async def test_normalization_uses_unicode_and_never_invents_statistics(self):
        def handler(request):
            return response(
                request,
                {
                    "kind": "youtube#searchListResponse",
                    "items": [
                        {
                            "kind": "youtube#searchResult",
                            "id": {"kind": "youtube#video", "videoId": "AbCdEf12345"},
                            "snippet": {
                                "title": "café 東京",
                                "publishedAt": "2026-11-08T12:00:00+00:00",
                            },
                        }
                    ],
                },
            )

        result, _, _ = await self.run_adapter(handler)

        record = result.records[0]
        self.assertEqual(record.title, "café 東京")
        self.assertIsNone(record.channel_id)
        self.assertIsNone(record.description)
        self.assertEqual(record.provider_video_id, "AbCdEf12345")
        self.assertEqual(record.canonical_video_url, "https://www.youtube.com/watch?v=AbCdEf12345")
        self.assertFalse(hasattr(record, "view_count"))
        self.assertEqual(record.metric.value, 1)

    async def test_malformed_response_fails_safely_without_leaking_credentials(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b"not-json",
                request=request,
            )

        result, _, _ = await self.run_adapter(handler)

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.queries[0].error.code, YouTubeErrorCode.MALFORMED_RESPONSE)
        self.assertNotIn(FAKE_API_KEY, repr(result))

    async def test_source_capability_is_offline_implemented_but_live_unapproved(self):
        from app.sources.catalog import YOUTUBE_SOURCE_ID, source_capability

        capability = source_capability(YOUTUBE_SOURCE_ID)
        self.assertEqual(capability.adapter_status, "IMPLEMENTED_OFFLINE_VERIFIED")
        self.assertEqual(capability.readiness, "PENDING_APPROVAL")
        self.assertEqual(capability.approval_status, "PENDING_APPROVAL")
        self.assertEqual(capability.configuration_status, "NOT_CONFIGURED")
        self.assertEqual(capability.live_verification_status, "NOT_VERIFIED")
        self.assertFalse(capability.default_enabled)
        self.assertNotIn("LIVE", capability.supported_data_modes)


if __name__ == "__main__":
    unittest.main()
