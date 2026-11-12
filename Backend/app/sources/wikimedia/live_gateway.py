"""Guarded Wikimedia top-per-country request; live transport is never used by tests."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

from app.sources.catalog import WIKIMEDIA_SOURCE_ID, source_capability
from app.sources.http_policy import HttpDeadlineExceeded, HttpPolicy, HttpResponseTooLarge
from app.sources.live_gateway import (
    ApprovalProvider,
    GatewayError,
    checked_source_approval,
)
from app.sources.wikimedia.adapter import _parse
from app.workers.guarded_attempt import GuardedAttemptRunner

_NETWORK_CONSTRUCTION = object()
_MAX_RESPONSE_BYTES = 1_000_000
_CONTACT = re.compile(r"\([^\r\n()]*?(?:https?://|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})[^\r\n()]*\)")


@dataclass(frozen=True)
class WikimediaRequest:
    observation_date: date
    plan_fingerprint: str


@dataclass(frozen=True)
class WikimediaPage:
    observation_date: date
    fetched_at: datetime
    data_mode: str
    http_status: int
    item_count: int
    rejected_count: int
    observations: tuple


class WikimediaGatewayError(GatewayError):
    def __init__(self, code, *, retry_after_seconds=None, retry_after_unbounded_seconds=None):
        self.retry_after_seconds = retry_after_seconds
        self.retry_after_unbounded_seconds = retry_after_unbounded_seconds
        super().__init__(code)


class _SharedTransport(httpx.AsyncBaseTransport):
    """Keep the gateway-owned connection pool alive across per-attempt clients."""

    def __init__(self, inner):
        self.inner = inner

    async def handle_async_request(self, request):
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        return None


class WikimediaTopPageGateway:
    """One fixed-source daily page, charged and fenced before outbound I/O."""

    def __init__(self, approvals: ApprovalProvider, runner: GuardedAttemptRunner, *,
                 user_agent: str, inner: httpx.AsyncBaseTransport, offline: bool,
                 clock=lambda: datetime.now(timezone.utc), policy: HttpPolicy | None = None,
                 _construction_token=None):
        if (not callable(getattr(approvals, "current", None))
                or not isinstance(runner, GuardedAttemptRunner) or not callable(clock)):
            raise ValueError("approval, guarded runner and clock are required")
        if (not isinstance(user_agent, str) or not user_agent.strip()
                or "\r" in user_agent or "\n" in user_agent or not _CONTACT.search(user_agent)):
            raise ValueError("Wikimedia User-Agent must include operator contact information")
        if type(offline) is not bool or (offline and not isinstance(inner, httpx.MockTransport)):
            raise ValueError("offline Wikimedia gateway requires MockTransport")
        if not offline and (type(inner) is not httpx.AsyncHTTPTransport
                            or _construction_token is not _NETWORK_CONSTRUCTION):
            raise ValueError("live Wikimedia gateway requires controlled HTTP transport")
        self.approvals, self.runner, self.user_agent = approvals, runner, user_agent
        self._inner, self._offline, self.clock = inner, offline, clock
        self.policy = policy or HttpPolicy(max_retries=0)
        if not isinstance(self.policy, HttpPolicy) or self.policy.max_retries != 0:
            raise ValueError("gateway makes one attempt; retries belong to the worker")
        self._closed = False

    @classmethod
    def offline_fixture(cls, approvals, runner, *, user_agent, transport, **kwargs):
        if not isinstance(transport, httpx.MockTransport):
            raise ValueError("offline Wikimedia gateway refuses real network transports")
        return cls(approvals, runner, user_agent=user_agent, inner=transport, offline=True, **kwargs)

    @classmethod
    async def controlled_network(cls, approvals, runner, *, user_agent, **kwargs):
        """Build a fixed-host transport after approval; this factory sends no request."""
        inner = httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)
        try:
            gateway = cls(approvals, runner, user_agent=user_agent, inner=inner, offline=False,
                          _construction_token=_NETWORK_CONSTRUCTION, **kwargs)
            gateway._check_approval()
            return gateway
        except BaseException:
            await inner.aclose()
            raise

    def _check_approval(self):
        return checked_source_approval(self.approvals, WIKIMEDIA_SOURCE_ID,
            self.runner.admission.policy.plan_fingerprint, self.clock)

    def _validate_request(self, request):
        now = self.clock()
        if (not isinstance(request, WikimediaRequest)
                or request.plan_fingerprint != self.runner.admission.policy.plan_fingerprint
                or not isinstance(request.observation_date, date)
                or isinstance(request.observation_date, datetime)
                or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
                or request.observation_date > now.astimezone(timezone.utc).date()
                or request.observation_date < now.astimezone(timezone.utc).date() - timedelta(days=30)):
            raise GatewayError("INVALID_SERVER_QUERY")
        return request.observation_date

    async def fetch_day(self, *, request, job_id, owner, fence, attempt_id, timeout_seconds=15,
                        on_page_receipt=None):
        if self._closed:
            raise GatewayError("GATEWAY_CLOSED")
        if on_page_receipt is not None and not callable(on_page_receipt):
            raise GatewayError("INVALID_RECEIPT_CALLBACK")
        approved = self._check_approval()
        day = self._validate_request(request)
        maximum = min(self.policy.total_timeout_seconds,
                      self.runner.admission.max_attempt_seconds(WIKIMEDIA_SOURCE_ID))
        if (type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= maximum):
            raise GatewayError("INVALID_TIMEOUT")
        endpoint = source_capability(WIKIMEDIA_SOURCE_ID).endpoint + f"/{day:%Y/%m/%d}"

        async def send():
            if self._check_approval() != approved:
                return ("SOURCE_NOT_APPROVED", None, None)
            try:
                async with httpx.AsyncClient(transport=_SharedTransport(self._inner), follow_redirects=False,
                                             trust_env=False) as client:
                    response = await self.policy.get_bounded(client, endpoint,
                        headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                        max_response_bytes=_MAX_RESPONSE_BYTES, total_timeout_seconds=timeout_seconds)
            except HttpResponseTooLarge:
                return ("RESPONSE_TOO_LARGE", None, None)
            except (HttpDeadlineExceeded, TimeoutError):
                return ("UPSTREAM_TIMEOUT", None, None)
            except httpx.RequestError:
                return ("UPSTREAM_ERROR", None, None)
            if response.status_code == 404:
                return ("DAY_UNAVAILABLE", None, None)
            if response.status_code == 429:
                header = response.headers.get("Retry-After")
                unbounded = HttpPolicy.parse_retry_after(header, 0, self.clock())
                delay = self.policy.retry_delay(header, 0, self.clock())
                return ("RATE_LIMITED", delay, unbounded)
            if response.status_code != 200:
                return ("UPSTREAM_ERROR", None, None)
            try:
                payload = response.json()
                fetched_at = self.clock().astimezone(timezone.utc)
                observations, count, rejected, _ = _parse(payload, day, fetched_at)
                if not observations and not rejected:
                    return ("EMPTY_RESPONSE", None, None)
                return ("PAGE", WikimediaPage(day, fetched_at,
                    "SYNTHETIC" if self._offline else "LIVE", response.status_code,
                    count, rejected, tuple(observations)), None)
            except (ValueError, TypeError, RecursionError):
                return ("MALFORMED_RESPONSE", None, None)

        def before_settle(value):
            code, page, _ = value
            if code == "PAGE":
                return on_page_receipt(page)

        outcome = await self.runner.run(job_id=job_id, owner=owner, fence=fence,
            source_id=WIKIMEDIA_SOURCE_ID, attempt_id=attempt_id,
            timeout_seconds=timeout_seconds, request=send,
            before_settle=before_settle if on_page_receipt is not None else None)
        if outcome.status != "COMPLETED":
            raise WikimediaGatewayError(outcome.status)
        if outcome.settlement_pending:
            raise WikimediaGatewayError("SETTLEMENT_PENDING")
        code, value, unbounded_delay = outcome.value
        if code == "PAGE":
            return value
        if code == "RATE_LIMITED":
            raise WikimediaGatewayError(code, retry_after_seconds=value,
                                        retry_after_unbounded_seconds=unbounded_delay)
        raise GatewayError(code)

    async def aclose(self):
        if not self._closed:
            self._closed = True
            await self._inner.aclose()

    @property
    def data_mode(self):
        return "SYNTHETIC" if self._offline else "LIVE"
