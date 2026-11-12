"""Live-capable boundary exercised only with MockTransport and no network."""

import asyncio
import logging
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from test_d5_public_admission import NOW, coordinator, policy

from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.live_gateway import (
    GatewayError,
    LivePageGateway,
    SourceApproval,
    _CredentialTransport,
)
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.pagination import build_contexts, initial_offset
from app.sources.youtube.pagination import build_execution_plan
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
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.control = coordinator(path, policy(plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint))
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

    async def test_network_factory_requires_approvals_and_opens_no_connection(self):
        gateway = await LivePageGateway.controlled_network(
            GOOGLE_SOURCE_ID, Credentials(), Approvals(GOOGLE_SOURCE_ID), self.runner,
            clock=lambda: NOW)
        try:
            # No fetch is made. This checks construction only, not live verification.
            self.assertEqual(gateway.data_mode, "LIVE")
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
