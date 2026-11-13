"""Guarded Wikimedia top-per-country request; live transport is never used by tests."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

from app.sources.catalog import WIKIMEDIA_SOURCE_ID, source_capability
from app.sources.http_policy import (
    HttpDeadlineExceeded,
    HttpPolicy,
    HttpRequestFailure,
    HttpResponseTooLarge,
)
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
    response_bytes: int = 0


@dataclass(frozen=True)
class WikimediaFailureDiagnostic:
    """Sanitized in-memory failure facts; deliberately excludes headers and body."""

    source_id: str
    failure_phase: str
    http_status: int | None
    exception_class: str | None
    bytes_received: int
    response_size_limit: int
    attempt_id: str
    error_code: str


class WikimediaGatewayError(GatewayError):
    def __init__(self, code, *, retry_after_seconds=None, retry_after_unbounded_seconds=None,
                 diagnostic: WikimediaFailureDiagnostic | None = None):
        self.retry_after_seconds = retry_after_seconds
        self.retry_after_unbounded_seconds = retry_after_unbounded_seconds
        self.diagnostic = diagnostic
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
                 before_request=None,
                 on_rate_limited=None,
                 _construction_token=None):
        if (not callable(getattr(approvals, "current", None))
                or not isinstance(runner, GuardedAttemptRunner) or not callable(clock)
                or (before_request is not None and not callable(before_request))
                or (on_rate_limited is not None and not callable(on_rate_limited))):
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
        self.before_request = before_request
        self.on_rate_limited = on_rate_limited
        self._inner, self._offline, self.clock = inner, offline, clock
        self.policy = policy or HttpPolicy(max_retries=0)
        if not isinstance(self.policy, HttpPolicy) or self.policy.max_retries != 0:
            raise ValueError("gateway makes one attempt; retries belong to the worker")
        self._closed = False
        self.last_diagnostic: WikimediaFailureDiagnostic | None = None

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
            self.runner.admission.policy.plan_fingerprint, self.clock, runner=self.runner)

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
        self.last_diagnostic = None
        if on_page_receipt is not None and not callable(on_page_receipt):
            raise GatewayError("INVALID_RECEIPT_CALLBACK")
        approved = self._check_approval()
        day = self._validate_request(request)
        maximum = min(self.policy.total_timeout_seconds,
                      self.runner.admission.max_attempt_seconds(WIKIMEDIA_SOURCE_ID))
        if (type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= maximum):
            raise GatewayError("INVALID_TIMEOUT")
        endpoint = source_capability(WIKIMEDIA_SOURCE_ID).endpoint + f"/{day:%Y/%m/%d}"

        def record_diagnostic(code: str, phase: str, *, http_status: int | None = None,
                              exception_class: str | None = None, bytes_received: int = 0,
                              response_size_limit: int = _MAX_RESPONSE_BYTES):
            diagnostic = WikimediaFailureDiagnostic(
                source_id=WIKIMEDIA_SOURCE_ID,
                failure_phase=phase,
                http_status=http_status,
                exception_class=exception_class,
                bytes_received=bytes_received,
                response_size_limit=response_size_limit,
                attempt_id=attempt_id,
                error_code=code,
            )
            self.last_diagnostic = diagnostic

        async def send():
            if self._check_approval() != approved:
                return ("SOURCE_NOT_APPROVED", None, None)
            if self.before_request is not None:
                await self.before_request()
            try:
                async with httpx.AsyncClient(transport=_SharedTransport(self._inner), follow_redirects=False,
                                             trust_env=False) as client:
                    response = await self.policy.get_bounded(client, endpoint,
                        headers={"User-Agent": self.user_agent, "Accept": "application/json",
                                 "Accept-Encoding": "identity"},
                        max_response_bytes=_MAX_RESPONSE_BYTES, total_timeout_seconds=timeout_seconds)
            except HttpResponseTooLarge as exc:
                record_diagnostic("RESPONSE_TOO_LARGE", exc.phase,
                    http_status=exc.status_code, bytes_received=exc.bytes_received,
                    response_size_limit=exc.max_response_bytes or _MAX_RESPONSE_BYTES)
                return ("RESPONSE_TOO_LARGE", None, None)
            except HttpDeadlineExceeded as exc:
                detail = exc.diagnostic
                record_diagnostic("UPSTREAM_TIMEOUT", detail.phase if detail else "BODY_STREAM",
                    http_status=detail.http_status if detail else None,
                    exception_class=detail.exception_class if detail else "HttpDeadlineExceeded",
                    bytes_received=detail.bytes_received if detail else 0,
                    response_size_limit=detail.max_response_bytes if detail else _MAX_RESPONSE_BYTES)
                return ("UPSTREAM_TIMEOUT", None, None)
            except HttpRequestFailure as exc:
                detail = exc.diagnostic
                code = "UPSTREAM_TIMEOUT" if detail.exception_class in {
                    "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException"
                } else "UPSTREAM_ERROR"
                record_diagnostic(code, detail.phase, http_status=detail.http_status,
                    exception_class=detail.exception_class, bytes_received=detail.bytes_received,
                    response_size_limit=detail.max_response_bytes)
                return (code, None, None)
            except httpx.TimeoutException:
                record_diagnostic("UPSTREAM_TIMEOUT", "REQUEST_DISPATCH",
                    exception_class="TimeoutException")
                return ("UPSTREAM_TIMEOUT", None, None)
            except httpx.RequestError:
                record_diagnostic("UPSTREAM_ERROR", "REQUEST_DISPATCH",
                    exception_class="RequestError")
                return ("UPSTREAM_ERROR", None, None)
            if response.status_code == 404:
                record_diagnostic("DAY_UNAVAILABLE", "RESPONSE_HEADERS",
                    http_status=response.status_code)
                return ("DAY_UNAVAILABLE", None, None)
            if response.status_code == 429:
                record_diagnostic("RATE_LIMITED", "RESPONSE_HEADERS",
                    http_status=response.status_code)
                header = response.headers.get("Retry-After")
                unbounded = HttpPolicy.parse_retry_after(header, 0, self.clock())
                delay = self.policy.retry_delay(header, 0, self.clock())
                if self.on_rate_limited is not None:
                    await self.on_rate_limited(unbounded if header else (delay or 1.0))
                return ("RATE_LIMITED", delay, unbounded)
            if response.status_code != 200:
                record_diagnostic("UPSTREAM_ERROR", "RESPONSE_HEADERS",
                    http_status=response.status_code)
                return ("UPSTREAM_ERROR", None, None)
            try:
                payload = response.json()
            except (ValueError, TypeError, RecursionError) as exc:
                record_diagnostic("MALFORMED_RESPONSE", "JSON_DECODE",
                    http_status=response.status_code, exception_class=type(exc).__name__,
                    bytes_received=len(response.content))
                return ("MALFORMED_RESPONSE", None, None)
            try:
                fetched_at = self.clock().astimezone(timezone.utc)
                observations, count, rejected, _ = _parse(payload, day, fetched_at)
                if not observations and not rejected:
                    record_diagnostic("EMPTY_RESPONSE", "SCHEMA_VALIDATION",
                        http_status=response.status_code, bytes_received=len(response.content))
                    return ("EMPTY_RESPONSE", None, None)
                return ("PAGE", WikimediaPage(day, fetched_at,
                    "SYNTHETIC" if self._offline else "LIVE", response.status_code,
                    count, rejected, tuple(observations), len(response.content)), None)
            except (ValueError, TypeError, RecursionError) as exc:
                record_diagnostic("MALFORMED_RESPONSE", "SCHEMA_VALIDATION",
                    http_status=response.status_code, exception_class=type(exc).__name__,
                    bytes_received=len(response.content))
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
            if outcome.status == "UPSTREAM_TIMEOUT" and self.last_diagnostic is None:
                record_diagnostic("UPSTREAM_TIMEOUT", "REQUEST_DISPATCH", exception_class="TimeoutError")
            raise WikimediaGatewayError(outcome.status, diagnostic=self.last_diagnostic)
        if outcome.settlement_pending:
            raise WikimediaGatewayError("SETTLEMENT_PENDING", diagnostic=self.last_diagnostic)
        code, value, unbounded_delay = outcome.value
        if code == "PAGE":
            return value
        if code == "RATE_LIMITED":
            raise WikimediaGatewayError(code, retry_after_seconds=value,
                                        retry_after_unbounded_seconds=unbounded_delay,
                                        diagnostic=self.last_diagnostic)
        raise WikimediaGatewayError(code, diagnostic=self.last_diagnostic)

    async def aclose(self):
        if not self._closed:
            self._closed = True
            await self._inner.aclose()

    @property
    def data_mode(self):
        return "SYNTHETIC" if self._offline else "LIVE"
