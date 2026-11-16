"""Live-capable boundary exercised only with MockTransport and no network."""

import asyncio
import logging
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from test_d5_public_admission import NOW, coordinator, policy

from app.jobs.public_admission import YOUTUBE_OTHER_POOL, PoolLimit
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.google_trends.trending_now import (
    TRENDING_NOW_ENGINE,
    GoogleTrendsRequest,
    build_trending_now_params,
)
from app.sources.live_gateway import (
    GatewayError,
    LivePageGateway,
    SourceApproval,
    _CredentialTransport,
)
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.pagination import build_contexts, initial_offset
from app.sources.youtube.models import YouTubeVideoListRequest
from app.sources.youtube.pagination import build_execution_plan
from app.sources.youtube.video_list import YOUTUBE_VIDEO_LIST_ENDPOINT
from app.workers.guarded_attempt import GuardedAttemptRunner

FAKE_SECRET = "fixture-secret-never-real"


class Credentials:
    def resolve(self, reference):
        if reference not in {"SERPAPI_API_KEY", "YOUTUBE_API_KEY"}:
            return None
        return FAKE_SECRET


class Approvals:
    def __init__(self, source):
        self.value = SourceApproval(source, DEFAULT_QUERY_PLAN.fingerprint,
            NOW + timedelta(hours=1), "policy-v1", approval_id="offline-fixture-approval",
            source_enabled=True, global_enabled=True,
            usage_approved=True, retention_approved=True, quota_approved=True,
            cost_approved=True, security_approved=True, distributed_controls_verified=True,
            egress_controls_verified=True, telemetry_controls_verified=True)

    def current(self, source):
        return self.value if self.value and self.value.source_id == source else None


class LiveGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = str(Path(self.directory.name) / "ledger.sqlite")
        selected_policy = policy(plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint)
        self.control = coordinator(path, replace(
            selected_policy,
            pools={**selected_policy.pools,
                   YOUTUBE_OTHER_POOL: PoolLimit(2, 3, 2, 3, 1)},
        ))
        self.job = self.control.admit("public-fetch-idempotency", now=NOW)["job_id"]
        self.fence = self.control.claim(self.job, "worker", now=NOW)["fence"]
        self.runner = GuardedAttemptRunner(self.control, clock=lambda: NOW)
        self.gateways = []

    async def asyncTearDown(self):
        for gateway in self.gateways:
            await gateway.aclose()
        self.directory.cleanup()

    def gateway(self, source, handler, approvals=None):
        gateway = LivePageGateway.offline_fixture(source, Credentials(), approvals or Approvals(source),
            self.runner, transport=httpx.MockTransport(handler), clock=lambda: NOW)
        self.gateways.append(gateway)
        return gateway

    def context(self, source):
        if source == YOUTUBE_SOURCE_ID:
            return build_execution_plan(DEFAULT_QUERY_PLAN, NOW).queries[0], None
        context = build_contexts(DEFAULT_QUERY_PLAN, source, NOW)[0]
        return context, initial_offset(context)

    def args(self, source, attempt_id="physical-attempt-0001"):
        context, cursor = self.context(source)
        return {"context": context, "cursor": cursor, "job_id": self.job, "owner": "worker",
                "fence": self.fence, "attempt_id": attempt_id, "timeout_seconds": 1}

    async def test_serp_google_and_bing_query_is_fixed_and_secret_is_not_logged(self):
        for index, source in enumerate((GOOGLE_SOURCE_ID, BING_SOURCE_ID)):
            seen = []
            def handler(request):
                seen.append(request)
                return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                    "organic_results": [{"position": 1, "link": "https://example.org/item",
                                         "title": "fixture"}]})
            gateway = self.gateway(source, handler)
            with self.assertLogs("httpx", logging.INFO) as logs:
                result = await gateway.fetch_page(**self.args(source, f"physical-attempt-{index:04d}"))
            self.assertEqual(result.data_mode, "SYNTHETIC")
            self.assertEqual(result.page.items[0]["title"], "fixture")
            self.assertEqual(seen[0].url.host, "serpapi.com")
            self.assertEqual(seen[0].url.params["api_key"], FAKE_SECRET)
            self.assertEqual(seen[0].headers["accept-encoding"], "identity")
            self.assertNotIn(FAKE_SECRET, " ".join(logs.output))
            self.assertNotIn("linen", " ".join(logs.output).lower())
            self.assertNotIn(FAKE_SECRET, repr(result))

    async def test_youtube_uses_header_and_fixture_never_claims_live(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"kind": "youtube#searchListResponse", "items": []})
        gateway = self.gateway(YOUTUBE_SOURCE_ID, handler)
        with self.assertLogs("httpx", logging.INFO) as logs:
            result = await gateway.fetch_page(**self.args(YOUTUBE_SOURCE_ID))
        self.assertEqual(result.data_mode, "SYNTHETIC")
        self.assertEqual(seen[0].headers["x-goog-api-key"], FAKE_SECRET)
        self.assertNotIn(FAKE_SECRET, str(seen[0].url))
        self.assertNotIn(FAKE_SECRET, " ".join(logs.output))

    async def test_videos_list_uses_fixed_endpoint_and_separate_other_method_budget(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={
                "kind": "youtube#videoListResponse",
                "items": [{
                    "kind": "youtube#video",
                    "id": "abcdefghijk",
                    "snippet": {
                        "title": "Offline chart fixture",
                        "channelId": "UCfixture_123",
                        "channelTitle": "Fixture channel",
                        "publishedAt": "2026-11-12T11:00:00Z",
                        "liveBroadcastContent": "none",
                    },
                }],
            })

        gateway = self.gateway(YOUTUBE_SOURCE_ID, handler)
        request = YouTubeVideoListRequest(
            "most_popular", self.control.policy.plan_fingerprint, max_results=10
        )
        result = await gateway.fetch_page(
            context=request,
            cursor=None,
            job_id=self.job,
            owner="worker",
            fence=self.fence,
            attempt_id="youtube-other-attempt-0001",
            timeout_seconds=1,
        )

        self.assertEqual(result.data_mode, "SYNTHETIC")
        self.assertEqual(result.page.items[0].video_id, "abcdefghijk")
        self.assertEqual(str(seen[0].url.copy_with(query=None)), YOUTUBE_VIDEO_LIST_ENDPOINT)
        self.assertEqual(seen[0].url.params["chart"], "mostPopular")
        self.assertEqual(seen[0].url.params["regionCode"], "US")
        self.assertEqual(seen[0].url.params["part"], "snippet")
        self.assertEqual(seen[0].url.params["maxResults"], "10")
        self.assertNotIn("statistics", seen[0].url.params["part"])
        self.assertEqual(seen[0].headers["x-goog-api-key"], FAKE_SECRET)
        self.assertEqual(self.control.quota_snapshot("youtube", now=NOW)["daily"]["used"], 0)
        metadata_request = YouTubeVideoListRequest(
            "metadata", self.control.policy.plan_fingerprint,
            video_ids=("abcdefghijk",),
        )
        await gateway.fetch_page(
            context=metadata_request,
            cursor=None,
            job_id=self.job,
            owner="worker",
            fence=self.fence,
            attempt_id="youtube-other-attempt-0002",
            timeout_seconds=1,
        )
        self.assertEqual(seen[1].url.path, "/youtube/v3/videos")
        self.assertEqual(seen[1].url.params["id"], "abcdefghijk")
        self.assertEqual(seen[1].url.params["part"], "snippet")
        self.assertNotIn("maxResults", seen[1].url.params)
        self.assertNotIn("regionCode", seen[1].url.params)
        self.assertEqual(self.control.quota_snapshot(
            YOUTUBE_OTHER_POOL, now=NOW
        )["daily"]["used"], 2)

    async def test_network_factory_requires_approvals_and_opens_no_connection(self):
        gateway = await LivePageGateway.controlled_network(
            GOOGLE_SOURCE_ID, Credentials(), Approvals(GOOGLE_SOURCE_ID), self.runner,
            clock=lambda: NOW)
        try:
            # No fetch is made. This checks construction only, not live verification.
            self.assertEqual(gateway.data_mode, "LIVE")
            context, cursor = self.context(GOOGLE_SOURCE_ID)
            params = gateway._params(context, cursor, 50)
            self.assertNotIn("no_cache", params)
            bing_gateway = await LivePageGateway.controlled_network(
                BING_SOURCE_ID, Credentials(), Approvals(BING_SOURCE_ID), self.runner,
                clock=lambda: NOW)
            try:
                bing_context, bing_cursor = self.context(BING_SOURCE_ID)
                self.assertEqual(
                    bing_gateway._params(bing_context, bing_cursor, 50)["no_cache"], "true"
                )
            finally:
                await bing_gateway.aclose()
            trend_request = GoogleTrendsRequest(
                GOOGLE_SOURCE_ID, self.control.policy.plan_fingerprint
            )
            self.assertNotIn("no_cache", gateway._params(trend_request, None, 50))
            forced = replace(trend_request, force_refresh=True)
            self.assertEqual(gateway._params(forced, None, 50)["no_cache"], "true")
        finally:
            await gateway.aclose()

    async def test_trending_now_uses_dedicated_parser_and_fixed_request(self):
        seen = []
        payload = {
            "search_metadata": {"status": "Success", "created_at": "2026-11-15T11:55:00Z",
                                "processed_at": "2026-11-15T11:55:02Z"},
            "search_parameters": {"engine": TRENDING_NOW_ENGINE, "geo": "US",
                                  "hl": "en", "hours": 24},
            "trending_searches": [{
                "query": "Portable picnic blanket", "start_timestamp": 1_794_739_200,
                "end_timestamp": None, "active": True, "search_volume": 200000,
                "search_volume_display": "200K+", "increase_percentage": 1100,
                "categories": [{"id": 185, "name": "Home & Garden"}],
                "trend_breakdown": ["portable picnic blanket", "folding picnic mat"],
            }],
        }

        def handle(request):
            seen.append(request)
            return httpx.Response(200, json=payload)

        gateway = self.gateway(GOOGLE_SOURCE_ID, handle)
        request = GoogleTrendsRequest(
            GOOGLE_SOURCE_ID, self.control.policy.plan_fingerprint
        )
        result = await gateway.fetch_page(
            context=request, cursor=None, job_id=self.job, owner="worker", fence=self.fence,
            attempt_id="trending-now-attempt-0001", timeout_seconds=1,
        )
        self.assertEqual(result.data_mode, "SYNTHETIC")
        self.assertEqual(result.page.records[0]["query"], "Portable picnic blanket")
        self.assertEqual(result.page.records[0]["search_volume_semantics"],
                         "PROVIDER_REPORTED_APPROXIMATE_BUCKET")
        self.assertEqual(seen[0].url.params["engine"], TRENDING_NOW_ENGINE)
        self.assertEqual({name: seen[0].url.params[name] for name in ("geo", "hl", "hours")},
                         {"geo": "US", "hl": "en", "hours": "24"})
        self.assertNotIn("no_cache", seen[0].url.params)

    async def test_trending_now_force_refresh_is_the_only_no_cache_path(self):
        request = GoogleTrendsRequest(
            GOOGLE_SOURCE_ID, self.control.policy.plan_fingerprint, force_refresh=True
        )
        self.assertEqual(build_trending_now_params(request), {
            "engine": TRENDING_NOW_ENGINE, "geo": "US", "hl": "en",
            "hours": 24, "output": "json",
        })
        gateway = await LivePageGateway.controlled_network(
            GOOGLE_SOURCE_ID, Credentials(), Approvals(GOOGLE_SOURCE_ID), self.runner,
            clock=lambda: NOW)
        try:
            self.assertEqual(gateway._params(request, None, 50)["no_cache"], "true")
        finally:
            await gateway.aclose()

    async def test_default_disabled_and_revocation_stop_before_network(self):
        seen = []
        approvals = Approvals(GOOGLE_SOURCE_ID)
        approvals.value = replace(approvals.value, security_approved=False)
        gateway = self.gateway(GOOGLE_SOURCE_ID, lambda r: seen.append(r), approvals)
        with self.assertRaisesRegex(GatewayError, "SOURCE_NOT_APPROVED"):
            await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        self.assertFalse(seen)
        with self.assertRaisesRegex(GatewayError, "SOURCE_NOT_APPROVED"):
            await LivePageGateway.controlled_network(GOOGLE_SOURCE_ID, Credentials(), approvals,
                                                     self.runner, clock=lambda: NOW)
        approvals.value = replace(approvals.value, security_approved=True)
        original = approvals.current
        def revoked_after_reservation(source):
            result = original(source)
            approvals.value = replace(approvals.value, source_enabled=False)
            return result
        with patch.object(approvals, "current", side_effect=revoked_after_reservation):
            with self.assertRaisesRegex(GatewayError, "SOURCE_NOT_APPROVED"):
                await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        self.assertFalse(seen)

    async def test_each_independent_approval_is_required(self):
        flags = ("source_enabled", "global_enabled", "usage_approved", "retention_approved",
                 "quota_approved", "cost_approved", "security_approved",
                 "distributed_controls_verified", "egress_controls_verified",
                 "telemetry_controls_verified")
        for flag in flags:
            seen = []
            approvals = Approvals(GOOGLE_SOURCE_ID)
            approvals.value = replace(approvals.value, **{flag: False})
            gateway = self.gateway(GOOGLE_SOURCE_ID, lambda r: seen.append(r), approvals)
            with self.subTest(flag=flag), self.assertRaisesRegex(GatewayError, "SOURCE_NOT_APPROVED"):
                await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
            self.assertEqual(seen, [])

    async def test_expired_wrong_scope_or_unidentified_approval_is_rejected(self):
        for changes in ({"expires_at": NOW}, {"plan_fingerprint": "b" * 64},
                        {"approval_id": ""}, {"policy_version": ""},
                        {"source_id": BING_SOURCE_ID}):
            approvals = Approvals(GOOGLE_SOURCE_ID)
            approvals.value = replace(approvals.value, **changes)
            gateway = self.gateway(GOOGLE_SOURCE_ID, lambda _r: httpx.Response(200), approvals)
            with self.subTest(changes=changes), self.assertRaisesRegex(GatewayError, "SOURCE_NOT_APPROVED"):
                await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))

    async def test_duplicate_attempt_never_calls_transport_twice(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                                             "organic_results": []})
        gateway = self.gateway(GOOGLE_SOURCE_ID, handler)
        await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        with self.assertRaisesRegex(GatewayError, "ALREADY_RESERVED"):
            await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        self.assertEqual(len(seen), 1)

    async def test_quota_exhaustion_prevents_outbound_request(self):
        from app.jobs.public_admission import AdmissionDenied

        for index in (1, 2):
            attempt = f"preexisting-attempt-{index:04d}"
            self.control.reserve_attempt(self.job, "worker", self.fence,
                source_id=GOOGLE_SOURCE_ID, attempt_id=attempt, now=NOW)
            self.control.settle_attempt(self.job, "worker", self.fence, attempt_id=attempt, now=NOW)
        seen = []
        gateway = self.gateway(BING_SOURCE_ID, lambda request: seen.append(request))
        with self.assertRaisesRegex(AdmissionDenied, "QUOTA_EXHAUSTED"):
            await gateway.fetch_page(**self.args(BING_SOURCE_ID))
        self.assertEqual(seen, [])

    async def test_rate_limit_is_classified_without_retries(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(429, headers={"Retry-After": "9"}, json={"error": "rate limited"})
        gateway = self.gateway(GOOGLE_SOURCE_ID, handler)
        with self.assertRaises(SerpApiError) as caught:
            await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        self.assertEqual(caught.exception.code, "RATE_LIMITED")
        self.assertEqual(caught.exception.retry_after, 9)
        self.assertEqual(len(seen), 1)

    async def test_transport_failure_never_exposes_exception_text(self):
        def handler(_request):
            raise RuntimeError("upstream failed with " + FAKE_SECRET)
        gateway = self.gateway(GOOGLE_SOURCE_ID, handler)
        with self.assertRaises(GatewayError) as caught:
            await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        self.assertEqual(caught.exception.code, "UPSTREAM_ERROR")
        self.assertNotIn(FAKE_SECRET, str(caught.exception))

    async def test_http_transport_failure_has_safe_phase_diagnostic(self):
        def handler(request):
            raise httpx.ConnectError("private connection detail " + FAKE_SECRET,
                                     request=request)
        gateway = self.gateway(GOOGLE_SOURCE_ID, handler)
        with self.assertRaisesRegex(GatewayError, "UPSTREAM_ERROR") as caught:
            await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic.source_id, GOOGLE_SOURCE_ID)
        self.assertEqual(diagnostic.failure_phase, "REQUEST_DISPATCH")
        self.assertIsNone(diagnostic.http_status)
        self.assertEqual(diagnostic.exception_class, "ConnectError")
        self.assertEqual(diagnostic.bytes_received, 0)
        self.assertEqual(diagnostic.error_code, "UPSTREAM_ERROR")
        self.assertNotIn(FAKE_SECRET, repr(diagnostic))

    async def test_body_stream_failure_is_distinguished_from_dispatch_failure(self):
        class FailingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"partial-private-content"
                raise httpx.ReadError("private body failure " + FAKE_SECRET)

            async def aclose(self):
                return None

        gateway = self.gateway(GOOGLE_SOURCE_ID, lambda request: httpx.Response(
            200, stream=FailingBody(), request=request))
        with self.assertRaisesRegex(GatewayError, "UPSTREAM_ERROR") as caught:
            await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic.failure_phase, "BODY_STREAM")
        self.assertEqual(diagnostic.http_status, 200)
        self.assertEqual(diagnostic.exception_class, "ReadError")
        self.assertEqual(diagnostic.bytes_received, len(b"partial-private-content"))
        self.assertNotIn(FAKE_SECRET, repr(diagnostic))

    async def test_uncertain_settlement_fails_closed_and_never_retries(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                                             "organic_results": []})
        gateway = self.gateway(GOOGLE_SOURCE_ID, handler)
        with patch.object(self.control, "settle_attempt", side_effect=RuntimeError("uncertain")):
            with self.assertRaisesRegex(GatewayError, "SETTLEMENT_PENDING"):
                await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        with self.assertRaisesRegex(GatewayError, "ALREADY_RESERVED"):
            await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        self.assertEqual(len(seen), 1)

    async def test_changed_approval_revision_does_not_send(self):
        seen = []
        approvals = Approvals(GOOGLE_SOURCE_ID)
        gateway = self.gateway(GOOGLE_SOURCE_ID, lambda r: seen.append(r), approvals)
        original = approvals.current
        def change_revision(source):
            result = original(source)
            approvals.value = replace(approvals.value, policy_version="policy-v2")
            return result
        with patch.object(approvals, "current", side_effect=change_revision):
            with self.assertRaisesRegex(GatewayError, "SOURCE_NOT_APPROVED"):
                await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        self.assertEqual(seen, [])

    async def test_cancellation_keeps_attempt_reserved_and_cannot_be_replayed(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler(request):
            calls.append(request)
            started.set()
            await release.wait()
            return httpx.Response(200, json={"search_metadata": {"status": "Success"},
                                             "organic_results": []})
        gateway = self.gateway(GOOGLE_SOURCE_ID, handler)
        task = asyncio.create_task(gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID)))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaisesRegex(GatewayError, "ALREADY_RESERVED"):
            await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
        self.assertEqual(len(calls), 1)

    async def test_timeout_is_bounded_and_sanitized(self):
        async def handler(_request):
            await asyncio.sleep(1)
            return httpx.Response(200)
        gateway = self.gateway(GOOGLE_SOURCE_ID, handler)
        args = {**self.args(GOOGLE_SOURCE_ID), "timeout_seconds": 0.01}
        with self.assertRaisesRegex(GatewayError, "UPSTREAM_TIMEOUT"):
            await gateway.fetch_page(**args)

    async def test_redirect_oversize_and_echoed_credentials_are_rejected(self):
        cases = [
            (httpx.Response(302, headers={"Location": "https://example.org/"}), "UNEXPECTED_REDIRECT"),
            (httpx.Response(200, content=b"x" * 1_000_001), "RESPONSE_TOO_LARGE"),
            (httpx.Response(200, text='{"organic_results":[{"title":"fixture-\\u0073ecret-never-real"}]}'), "SENSITIVE_UPSTREAM_RESPONSE"),
        ]
        for index, (response, code) in enumerate(cases):
            # Use independent source quotas/jobs per subcase.
            if index:
                await self.asyncTearDown()
                await self.asyncSetUp()
            gateway = self.gateway(GOOGLE_SOURCE_ID, lambda _request: response)
            with self.subTest(code=code):
                with self.assertRaises((GatewayError, SerpApiError)) as caught:
                    await gateway.fetch_page(**self.args(GOOGLE_SOURCE_ID))
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(FAKE_SECRET, str(caught.exception))

    async def test_invalid_context_and_untrusted_destinations_fail_closed(self):
        gateway = self.gateway(GOOGLE_SOURCE_ID, lambda _request: httpx.Response(500))
        args = self.args(GOOGLE_SOURCE_ID)
        args["context"] = replace(args["context"], canonical_query="unregistered query")
        with self.assertRaisesRegex(GatewayError, "INVALID_SERVER_QUERY"):
            await gateway.fetch_page(**args)
        transport = _CredentialTransport(GOOGLE_SOURCE_ID, FAKE_SECRET, httpx.MockTransport(
            lambda _request: httpx.Response(500)), {})
        with self.assertRaisesRegex(GatewayError, "OUTBOUND_DESTINATION_REJECTED"):
            await transport.handle_async_request(httpx.Request("GET", "https://attacker.invalid/"))
        request = httpx.Request("GET", "https://serpapi.com/search.json",
                                extensions={"trendscout_params": {"q": "injected"}})
        with self.assertRaisesRegex(GatewayError, "OUTBOUND_PARAMETERS_REJECTED"):
            await transport.handle_async_request(request)
