"""Fixed-host, separately admitted Google Trends RSS transport."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone

import httpx

from app.sources.catalog import GOOGLE_SOURCE_ID
from app.sources.google_trends.rss import RSS_ENDPOINT, parse_google_trends_rss
from app.sources.http_policy import HttpPolicy
from app.sources.live_gateway import (
    GatewayError,
    GatewayPage,
    checked_source_approval,
)
from app.sources.serpapi.errors import SerpApiError
from app.workers.guarded_attempt import GuardedAttemptRunner

_NETWORK_CONSTRUCTION = object()


class _SharedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner):
        self.inner = inner

    async def handle_async_request(self, request):
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        return None


class GoogleTrendsRSSGateway:
    """Fetch only the official US feed through a separately budgeted guard."""

    def __init__(self, approvals, runner: GuardedAttemptRunner, *, inner,
                 offline: bool, clock=lambda: datetime.now(timezone.utc),
                 policy: HttpPolicy | None = None, _construction_token=None):
        if (not callable(getattr(approvals, "current", None))
                or not isinstance(runner, GuardedAttemptRunner)
                or GOOGLE_SOURCE_ID not in runner.admission.policy.enabled_sources
                or type(offline) is not bool
                or offline and not isinstance(inner, httpx.MockTransport)
                or not offline and (type(inner) is not httpx.AsyncHTTPTransport
                    or _construction_token is not _NETWORK_CONSTRUCTION)
                or not callable(clock)):
            raise ValueError("RSS requires an approved Google source and controlled transport")
        if not callable(runner.admission.max_attempt_seconds):
            raise ValueError("guarded attempt admission is required")
        # Validate that the non-SerpApi network budget is explicitly configured.
        runner.admission.max_attempt_seconds(GOOGLE_SOURCE_ID, "google_rss")
        self.approvals, self.runner, self._inner = approvals, runner, inner
        self.offline, self.clock = offline, clock
        self.policy = policy or HttpPolicy(max_retries=0)
        if not isinstance(self.policy, HttpPolicy) or self.policy.max_retries != 0:
            raise ValueError("RSS gateway performs one reserved physical attempt")
        self._closed = False
        self._shared_transport = _SharedTransport(inner)
        self._client = httpx.AsyncClient(
            transport=self._shared_transport, follow_redirects=False, trust_env=False
        )

    @classmethod
    def offline_fixture(cls, approvals, runner, *, transport, **kwargs):
        if not isinstance(transport, httpx.MockTransport):
            raise ValueError("RSS offline fixture requires MockTransport")
        return cls(approvals, runner, inner=transport, offline=True, **kwargs)

    @classmethod
    async def controlled_network(cls, approvals, runner, **kwargs):
        inner = httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)
        try:
            gateway = cls(approvals, runner, inner=inner, offline=False,
                          _construction_token=_NETWORK_CONSTRUCTION, **kwargs)
            gateway._check_approval()
            return gateway
        except BaseException:
            await inner.aclose()
            raise

    @property
    def data_mode(self):
        return "SYNTHETIC" if self.offline else "LIVE"

    def _check_approval(self):
        return checked_source_approval(
            self.approvals, GOOGLE_SOURCE_ID,
            self.runner.admission.policy.plan_fingerprint, self.clock,
            runner=self.runner,
        )

    async def fetch_page(self, *, job_id, owner, fence, attempt_id,
                         timeout_seconds=15, on_page_receipt=None):
        if self._closed:
            raise GatewayError("GATEWAY_CLOSED")
        if on_page_receipt is not None and not callable(on_page_receipt):
            raise GatewayError("INVALID_RECEIPT_CALLBACK")
        approved = self._check_approval()
        parsed = []

        async def parse(response):
            page = parse_google_trends_rss(
                response, fetched_at=self.clock(), data_mode=self.data_mode
            )
            receipt = GatewayPage(GOOGLE_SOURCE_ID, self.data_mode, page)
            if on_page_receipt is not None:
                value = await asyncio.to_thread(on_page_receipt, receipt)
                if inspect.isawaitable(value):
                    await value
            return receipt

        async def before_settle(response):
            if not isinstance(response, httpx.Response):
                return
            try:
                parsed.append((await parse(response), None))
            except SerpApiError as error:
                parsed.append((None, error))

        async def request():
            if self._check_approval() != approved:
                return ("SOURCE_NOT_APPROVED", None)
            response = await self.policy.get_bounded(
                self._client, RSS_ENDPOINT,
                headers={"Accept": "application/rss+xml, application/xml;q=0.9"},
                max_response_bytes=1_000_000, total_timeout_seconds=timeout_seconds,
            )
            return ("RESPONSE", response)

        outcome = await self.runner.run(
            job_id=job_id, owner=owner, fence=fence, source_id=GOOGLE_SOURCE_ID,
            pool_name="google_rss", attempt_id=attempt_id,
            timeout_seconds=timeout_seconds, request=request, before_settle=before_settle,
        )
        if outcome.status != "COMPLETED":
            raise GatewayError(outcome.status)
        if outcome.settlement_pending and not parsed:
            raise GatewayError("SETTLEMENT_PENDING")
        code, response = outcome.value
        if code != "RESPONSE":
            raise GatewayError(code)
        if parsed:
            receipt, error = parsed[0]
            if error is not None:
                raise error
            return receipt
        return await parse(response)

    async def aclose(self):
        if not self._closed:
            self._closed = True
            await self._client.aclose()
            await self._inner.aclose()
