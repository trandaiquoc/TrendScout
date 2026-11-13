"""Guarded Wikimedia pageview contract tests; transport stays synthetic."""

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from offline_http_guard import strict_offline_http_guard
from test_d5_live_gateway import Approvals
from test_d5_public_admission import NOW, coordinator, policy

from app.jobs.public_admission import PoolLimit
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.live_gateway import GatewayError
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaRequest, WikimediaTopPageGateway
from app.workers.guarded_attempt import GuardedAttemptRunner

USER_AGENT = "TrendScoutTestBot/0.1 (https://example.invalid/contact)"


def enabled_policy():
    pools = {"serpapi": PoolLimit(10, 20, 10, 20, 1),
             "youtube": PoolLimit(10, 20, 10, 20, 1),
             "wikimedia": PoolLimit(10, 20, 10, 20, 1)}
    return policy(enabled_sources=frozenset({GOOGLE_SOURCE_ID, BING_SOURCE_ID,
        YOUTUBE_SOURCE_ID, WIKIMEDIA_SOURCE_ID}), pools=pools,
        plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint)


def response(day):
    return {"items": [{"country": "US", "access": "all-access",
        "year": day.strftime("%Y"), "month": day.strftime("%m"), "day": day.strftime("%d"),
        "articles": [{"article": "Coffee", "project": "en.wikipedia", "views_ceil": 100,
                      "rank": 1}]}]}


class WikimediaGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "state.sqlite")
        self.admission = coordinator(self.path, enabled_policy())
        self.job = self.admission.admit("wikimedia-fetch-idempotency", now=NOW)["job_id"]
        self.fence = self.admission.claim(self.job, "worker", now=NOW)["fence"]
        self.runner = GuardedAttemptRunner(self.admission, clock=lambda: NOW)
        self.approvals = Approvals(WIKIMEDIA_SOURCE_ID)
        self.request = WikimediaRequest(NOW.date(), DEFAULT_QUERY_PLAN.fingerprint)
        self.gateways = []

    async def asyncTearDown(self):
        for gateway in self.gateways:
            await gateway.aclose()
        self.directory.cleanup()

    def gateway(self, handler, approvals=None, user_agent=USER_AGENT):
        instance = WikimediaTopPageGateway.offline_fixture(
            approvals or self.approvals, self.runner, user_agent=user_agent,
            transport=httpx.MockTransport(handler), clock=lambda: NOW)
        self.gateways.append(instance)
        return instance

    def args(self, attempt_id="wikimedia-attempt-0001", **changes):
        return {"request": changes.pop("request", self.request), "job_id": self.job,
                "owner": "worker", "fence": self.fence, "attempt_id": attempt_id,
                "timeout_seconds": 1, **changes}

    async def test_fixed_daily_endpoint_uses_approval_quota_and_synthetic_mode(self):
        seen = []
        gateway = self.gateway(lambda req: (seen.append(req) or httpx.Response(
            200, json=response(self.request.observation_date))))
        result = await gateway.fetch_day(**self.args())
        self.assertEqual(result.data_mode, "SYNTHETIC")
        self.assertEqual(result.observations[0].canonical_keyword, "Coffee")
        self.assertEqual(str(seen[0].url),
            "https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/US/all-access/2026/11/12")
        self.assertEqual(seen[0].headers["user-agent"], USER_AGENT)
        self.assertEqual(seen[0].headers["accept-encoding"], "identity")
        self.assertEqual(str(seen[0].url.params), "")

    async def test_revoked_approval_and_invalid_scope_stop_before_transport(self):
        seen = []
        approval = Approvals(WIKIMEDIA_SOURCE_ID)
        approval.value = replace(approval.value, usage_approved=False)
        gateway = self.gateway(lambda req: (seen.append(req) or httpx.Response(200)), approval)
        with self.assertRaisesRegex(GatewayError, "SOURCE_NOT_APPROVED"):
            await gateway.fetch_day(**self.args())
        approval.value = replace(approval.value, usage_approved=True)
        with self.assertRaisesRegex(GatewayError, "INVALID_SERVER_QUERY"):
            await gateway.fetch_day(**self.args("wikimedia-attempt-0002",
                request=WikimediaRequest(self.request.observation_date, "f" * 64)))
        self.assertEqual(seen, [])

    async def test_duplicate_attempt_never_sends_second_request(self):
        seen = []
        gateway = self.gateway(lambda req: (seen.append(req) or httpx.Response(
            200, json=response(self.request.observation_date))))
        await gateway.fetch_day(**self.args())
        with self.assertRaisesRegex(GatewayError, "ALREADY_RESERVED"):
            await gateway.fetch_day(**self.args())
        self.assertEqual(len(seen), 1)

    async def test_durable_page_receipt_callback_runs_before_settlement(self):
        events = []
        gateway = self.gateway(lambda _req: httpx.Response(
            200, json=response(self.request.observation_date)))
        original_settle = self.admission.settle_attempt

        def settle(*args, **kwargs):
            events.append("settle")
            return original_settle(*args, **kwargs)

        def persist(page):
            self.assertEqual(page.observations[0].canonical_keyword, "Coffee")
            events.append("receipt")

        with patch.object(self.admission, "settle_attempt", settle):
            await gateway.fetch_day(**self.args(on_page_receipt=persist))
        self.assertEqual(events, ["receipt", "settle"])

    async def test_receipt_write_failure_keeps_wikimedia_attempt_reserved(self):
        requests = []
        gateway = self.gateway(lambda request: (
            requests.append(request) or httpx.Response(
                200, json=response(self.request.observation_date))))

        def fail_receipt(_page):
            raise OSError("synthetic checkpoint unavailable")

        with self.assertRaisesRegex(GatewayError, "RESULT_PERSISTENCE_FAILED"):
            await gateway.fetch_day(**self.args(on_page_receipt=fail_receipt))
        with self.assertRaisesRegex(GatewayError, "ALREADY_RESERVED"):
            await gateway.fetch_day(**self.args(on_page_receipt=lambda _page: None))
        self.assertEqual(len(requests), 1)

    async def test_shared_transport_remains_open_for_a_later_attempt(self):
        seen = []
        gateway = self.gateway(lambda req: (seen.append(req) or httpx.Response(
            200, json=response(self.request.observation_date))))
        first = await gateway.fetch_day(**self.args("wikimedia-attempt-0001"))
        second = await gateway.fetch_day(**self.args("wikimedia-attempt-0002"))
        self.assertEqual(first.data_mode, "SYNTHETIC")
        self.assertEqual(second.data_mode, "SYNTHETIC")
        self.assertEqual(len(seen), 2)

    async def test_rate_limit_is_reported_with_retry_after_and_no_retry(self):
        seen = []
        gateway = self.gateway(lambda req: (seen.append(req) or httpx.Response(
            429, headers={"Retry-After": "17"})))
        with self.assertRaisesRegex(GatewayError, "RATE_LIMITED") as caught:
            await gateway.fetch_day(**self.args())
        self.assertEqual(caught.exception.retry_after_seconds, 17)
        self.assertEqual(len(seen), 1)

    async def test_not_found_does_not_fall_back_to_another_date(self):
        seen = []
        gateway = self.gateway(lambda request: (
            seen.append(request) or httpx.Response(404)))
        with self.assertRaisesRegex(GatewayError, "DAY_UNAVAILABLE"):
            await gateway.fetch_day(**self.args())
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].url.path.endswith("/2026/11/12"))
        with self.assertRaisesRegex(GatewayError, "ALREADY_RESERVED"):
            await gateway.fetch_day(**self.args())
        self.assertEqual(len(seen), 1)

    async def test_wrong_scope_or_date_is_rejected_without_retry(self):
        seen = []
        cases = []
        wrong_country = response(self.request.observation_date)
        wrong_country["items"][0]["country"] = "CA"
        cases.append(wrong_country)
        wrong_access = response(self.request.observation_date)
        wrong_access["items"][0]["access"] = "desktop"
        cases.append(wrong_access)
        wrong_date = response(self.request.observation_date)
        wrong_date["items"][0]["day"] = "11"
        cases.append(wrong_date)
        malformed_schema = {"items": [{"country": "US"}]}
        cases.append(malformed_schema)

        for payload in cases:
            attempt_id = f"wikimedia-schema-attempt-{len(seen) + 1:04d}"
            gateway = self.gateway(lambda request, body=payload: (
                seen.append(request) or httpx.Response(200, json=body)))
            with self.assertRaisesRegex(GatewayError, "MALFORMED_RESPONSE"):
                await gateway.fetch_day(**self.args(attempt_id))
            self.assertEqual(gateway.last_diagnostic.failure_phase, "SCHEMA_VALIDATION")
        self.assertEqual(len(seen), len(cases))

    async def test_body_read_error_after_200_is_sanitized_and_remains_reserved(self):
        class FailingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"private-partial-response"
                raise httpx.ReadError("private transport detail")

            async def aclose(self):
                return None

        gateway = self.gateway(lambda request: httpx.Response(
            200, stream=FailingBody(), request=request))
        with self.assertRaisesRegex(GatewayError, "UPSTREAM_ERROR") as caught:
            await gateway.fetch_day(**self.args())
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic.source_id, WIKIMEDIA_SOURCE_ID)
        self.assertEqual(diagnostic.failure_phase, "BODY_STREAM")
        self.assertEqual(diagnostic.http_status, 200)
        self.assertEqual(diagnostic.exception_class, "ReadError")
        self.assertEqual(diagnostic.bytes_received, len(b"private-partial-response"))
        self.assertEqual(diagnostic.error_code, "UPSTREAM_ERROR")
        self.assertEqual(diagnostic.attempt_id, "wikimedia-attempt-0001")
        self.assertNotIn("private", repr(diagnostic))
        self.assertNotIn(b"private-partial-response", Path(self.path).read_bytes())
        with self.assertRaisesRegex(GatewayError, "ALREADY_RESERVED"):
            await gateway.fetch_day(**self.args())

    async def test_remote_protocol_and_timeout_are_distinguished_from_json_errors(self):
        class ProtocolFailure(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"{"
                raise httpx.RemoteProtocolError("private protocol detail")

            async def aclose(self):
                return None

        protocol_gateway = self.gateway(lambda request: httpx.Response(
            200, stream=ProtocolFailure(), request=request))
        with self.assertRaisesRegex(GatewayError, "UPSTREAM_ERROR") as protocol_error:
            await protocol_gateway.fetch_day(**self.args())
        self.assertEqual(protocol_error.exception.diagnostic.exception_class, "RemoteProtocolError")
        self.assertEqual(protocol_error.exception.diagnostic.failure_phase, "BODY_STREAM")

        class TimeoutBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise httpx.ReadTimeout("private timeout detail")
                yield b""

            async def aclose(self):
                return None

        timeout_gateway = self.gateway(lambda request: httpx.Response(
            200, stream=TimeoutBody(), request=request))
        with self.assertRaisesRegex(GatewayError, "UPSTREAM_TIMEOUT") as timeout_error:
            await timeout_gateway.fetch_day(**self.args("wikimedia-attempt-timeout-0001"))
        self.assertEqual(timeout_error.exception.diagnostic.exception_class, "ReadTimeout")
        self.assertEqual(timeout_error.exception.diagnostic.http_status, 200)

    async def test_complete_invalid_json_is_malformed_not_transport_error(self):
        gateway = self.gateway(lambda request: httpx.Response(
            200, content=b"{not-json", request=request))
        with self.assertRaisesRegex(GatewayError, "MALFORMED_RESPONSE") as caught:
            await gateway.fetch_day(**self.args())
        self.assertEqual(caught.exception.diagnostic.failure_phase, "JSON_DECODE")
        self.assertEqual(caught.exception.diagnostic.http_status, 200)
        self.assertEqual(caught.exception.diagnostic.exception_class, "JSONDecodeError")

    async def test_dispatch_and_content_decode_failures_have_distinct_sanitized_details(self):
        dispatch_gateway = self.gateway(lambda request: (_ for _ in ()).throw(
            httpx.ConnectError("private connection detail", request=request)))
        with self.assertRaisesRegex(GatewayError, "UPSTREAM_ERROR") as dispatch_error:
            await dispatch_gateway.fetch_day(**self.args())
        self.assertEqual(dispatch_error.exception.diagnostic.failure_phase, "REQUEST_DISPATCH")
        self.assertIsNone(dispatch_error.exception.diagnostic.http_status)
        self.assertEqual(dispatch_error.exception.diagnostic.exception_class, "ConnectError")

        decode_gateway = self.gateway(lambda request: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=b"not-gzip", request=request))
        with self.assertRaisesRegex(GatewayError, "UPSTREAM_ERROR") as decode_error:
            await decode_gateway.fetch_day(**self.args("wikimedia-attempt-decode-0001"))
        self.assertEqual(decode_error.exception.diagnostic.failure_phase, "REQUEST_DISPATCH")
        self.assertIsNone(decode_error.exception.diagnostic.http_status)
        self.assertEqual(decode_error.exception.diagnostic.exception_class, "DecodingError")

    async def test_gateway_cancellation_propagates_and_preserves_attempt_reservation(self):
        started = asyncio.Event()

        class HangingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                return None

        gateway = self.gateway(lambda request: httpx.Response(
            200, stream=HangingBody(), request=request))
        task = asyncio.create_task(gateway.fetch_day(**self.args()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaisesRegex(GatewayError, "ALREADY_RESERVED"):
            await gateway.fetch_day(**self.args())

    async def test_oversized_response_is_rejected_and_reserved(self):
        seen = []
        gateway = self.gateway(lambda request: (
            seen.append(request) or httpx.Response(200, content=b"x" * 1_000_001)))
        with self.assertRaisesRegex(GatewayError, "RESPONSE_TOO_LARGE"):
            await gateway.fetch_day(**self.args())
        self.assertEqual(gateway.last_diagnostic.failure_phase, "RESPONSE_HEADERS")
        self.assertEqual(gateway.last_diagnostic.http_status, 200)
        self.assertEqual(gateway.last_diagnostic.response_size_limit, 1_000_000)
        self.assertEqual(len(seen), 1)
        with self.assertRaisesRegex(GatewayError, "ALREADY_RESERVED"):
            await gateway.fetch_day(**self.args())
        self.assertEqual(len(seen), 1)

    async def test_long_retry_after_is_preserved_for_durable_deferral(self):
        gateway = self.gateway(lambda _req: httpx.Response(
            429, headers={"Retry-After": "91"}))
        with self.assertRaisesRegex(GatewayError, "RATE_LIMITED") as caught:
            await gateway.fetch_day(**self.args())
        self.assertIsNone(caught.exception.retry_after_seconds)
        self.assertEqual(caught.exception.retry_after_unbounded_seconds, 91)

    async def test_missing_contact_in_user_agent_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "contact information"):
            self.gateway(lambda _req: httpx.Response(200), user_agent="TrendScoutBot/1.0")

    async def test_controlled_network_constructor_opens_no_request(self):
        with strict_offline_http_guard() as (guard, fake_transport):
            tasks_before = set(asyncio.all_tasks())
            gateway = await WikimediaTopPageGateway.controlled_network(
                self.approvals, self.runner, user_agent=USER_AGENT, clock=lambda: NOW)
            self.gateways.append(gateway)
            self.assertEqual(gateway.data_mode, "LIVE")
            self.assertEqual(guard["real_constructions"], 0)
            self.assertEqual(guard["fake_constructions"], 1)
            self.assertEqual(guard["requests"], 0)
            self.assertEqual(guard["closed"], 0)
            self.assertEqual(fake_transport.__name__, "OfflineAsyncHTTPTransport")
            self.assertEqual(gateway._inner.kwargs,
                {"verify": True, "trust_env": False, "retries": 0})

            class MissingApproval:
                def current(self, _source):
                    return None

            expired = replace(self.approvals.value, expires_at=NOW)
            wrong_scope = replace(self.approvals.value, scope="UNRECOGNIZED")
            for invalid in (MissingApproval(),
                            type("ExpiredApproval", (), {"current": lambda _self, _source: expired})(),
                            type("WrongScopeApproval", (), {"current": lambda _self, _source: wrong_scope})()):
                before_closed = guard["closed"]
                with self.assertRaisesRegex(GatewayError, "SOURCE_NOT_APPROVED"):
                    await WikimediaTopPageGateway.controlled_network(
                        invalid, self.runner, user_agent=USER_AGENT, clock=lambda: NOW)
                self.assertEqual(guard["closed"], before_closed + 1)

            # Initialization happens after the fake transport is allocated; failure must close it.
            before_closed = guard["closed"]
            with self.assertRaisesRegex(ValueError, "contact information"):
                await WikimediaTopPageGateway.controlled_network(
                    self.approvals, self.runner, user_agent="invalid", clock=lambda: NOW)
            self.assertEqual(guard["closed"], before_closed + 1)
            self.assertEqual(guard["real_constructions"], 0)
            self.assertEqual(guard["requests"], 0)

            await gateway.aclose()
            self.assertEqual(guard["closed"], before_closed + 2)
            self.assertEqual(set(asyncio.all_tasks()), tasks_before)

            # A constructor that raises before returning owns no transport to clean up.
            current_fake = httpx.AsyncHTTPTransport

            class FailingTransport:
                def __init__(self, **_kwargs):
                    raise RuntimeError("offline fake constructor failure")

            with patch("httpx.AsyncHTTPTransport", FailingTransport):
                closed_before = guard["closed"]
                with self.assertRaisesRegex(RuntimeError, "offline fake constructor failure"):
                    await WikimediaTopPageGateway.controlled_network(
                        self.approvals, self.runner, user_agent=USER_AGENT, clock=lambda: NOW)
                self.assertEqual(guard["closed"], closed_before)
            self.assertIs(httpx.AsyncHTTPTransport, current_fake)
            self.assertEqual(guard["real_constructions"], 0)


if __name__ == "__main__":
    unittest.main()
