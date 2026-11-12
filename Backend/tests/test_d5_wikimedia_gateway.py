"""Guarded Wikimedia pageview contract tests; transport stays synthetic."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
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
        gateway = await WikimediaTopPageGateway.controlled_network(
            self.approvals, self.runner, user_agent=USER_AGENT, clock=lambda: NOW)
        self.gateways.append(gateway)
        self.assertEqual(gateway.data_mode, "LIVE")


if __name__ == "__main__":
    unittest.main()
