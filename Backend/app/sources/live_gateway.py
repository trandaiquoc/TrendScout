"""Fixed-host, single-page live-capable gateway; not wired into the application.

The offline constructor only accepts MockTransport and always labels data
SYNTHETIC. The network constructor requires explicit backend approvals and a
guarded attempt runner. Deployment-wide logging/egress review remains required.
"""

import asyncio
import html
import inspect
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import unquote

import httpx

from app.ports.credentials import CredentialProvider
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
)
from app.sources.http_policy import HttpPolicy
from app.sources.query_plan import get_query_plan
from app.sources.serpapi.client import SerpApiClient, build_request_params
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.models import QueryContext as SerpContext
from app.sources.serpapi.quota import SharedQuotaPool
from app.sources.youtube.client import YouTubeSearchClient, build_search_params
from app.sources.youtube.errors import YouTubeSourceError
from app.sources.youtube.models import QueryContext as YouTubeContext
from app.sources.youtube.pagination import build_execution_plan
from app.workers.guarded_attempt import GuardedAttemptRunner

_MAX_RESPONSE_BYTES = 1_000_000
_SOURCES = frozenset({GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID})
_NETWORK_CONSTRUCTION = object()


def _contains_secret(value: str, secret: str) -> bool:
    for _ in range(4):
        if secret in value:
            return True
        decoded = html.unescape(unquote(value))
        if decoded == value:
            break
        value = decoded
    return secret in value


class GatewayError(Exception):
    """Stable error code without upstream URL, response body or credential."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def checked_source_approval(approvals, source_id, plan_fingerprint, clock, *, runner=None):
    """Validate a current server-owned source approval without leaking provider detail."""
    try:
        approval = approvals.current(source_id)
        now = clock()
        valid = (isinstance(approval, SourceApproval) and approval.source_id == source_id
                 and approval.plan_fingerprint == plan_fingerprint
                 and isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None
                 and isinstance(approval.expires_at, datetime) and approval.expires_at.tzinfo is not None
                 and approval.expires_at.utcoffset() is not None and approval.expires_at > now
                 and isinstance(approval.policy_version, str) and bool(approval.policy_version.strip())
                 and isinstance(approval.approval_id, str) and bool(approval.approval_id.strip())
                 and all(value is True for value in (approval.source_enabled, approval.global_enabled,
                     approval.usage_approved, approval.retention_approved, approval.quota_approved,
                     approval.cost_approved, approval.security_approved,
                     approval.egress_controls_verified, approval.telemetry_controls_verified)))
        if valid and approval.scope == "PRODUCTION":
            valid = approval.distributed_controls_verified is True
        elif valid and approval.scope == "LOCAL_SMOKE":
            from dataclasses import replace

            from app.local_smoke_policy import local_smoke_policy
            from app.storage.local_smoke_store import LocalSmokeStore
            from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
            from app.storage.local_volatile_checkpoints import LocalVolatileCheckpoints
            local_store = runner.admission.store if isinstance(runner, GuardedAttemptRunner) else None
            backing_store = local_store.store if type(local_store) is LocalSmokeStore else None
            if type(backing_store) is LocalVolatileCheckpoints:
                backing_store = backing_store.store
            valid = (not os.environ.get("VERCEL")
                     and approval.local_controls_verified is True
                     and isinstance(runner, GuardedAttemptRunner)
                     and type(runner.admission.store) is LocalSmokeStore
                     and type(backing_store) is LocalSQLiteAtomicStateStore
                     and backing_store.data_mode == "LIVE"
                     and runner.admission.policy == replace(local_smoke_policy(), enabled=True))
        elif valid and approval.scope == "LOCAL_NORMAL":
            from app.normal_wikimedia_live_policy import (
                POLICY_VERSION,
                normal_wikimedia_live_policy,
            )
            from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
            store = runner.admission.store if isinstance(runner, GuardedAttemptRunner) else None
            valid = (not os.environ.get("VERCEL")
                     and source_id == WIKIMEDIA_SOURCE_ID
                     and approval.policy_version == POLICY_VERSION
                     and approval.local_controls_verified is True
                     and isinstance(runner, GuardedAttemptRunner)
                     and type(store) is LocalSQLiteAtomicStateStore
                     and store.data_mode == "LIVE"
                     and runner.admission.policy == normal_wikimedia_live_policy())
        else:
            valid = False
    except Exception:
        valid = False
    if not valid:
        raise GatewayError("SOURCE_NOT_APPROVED")
    return approval


@dataclass(frozen=True)
class SourceApproval:
    source_id: str
    plan_fingerprint: str
    expires_at: datetime
    policy_version: str
    approval_id: str = ""
    source_enabled: bool = False
    global_enabled: bool = False
    usage_approved: bool = False
    retention_approved: bool = False
    quota_approved: bool = False
    cost_approved: bool = False
    security_approved: bool = False
    distributed_controls_verified: bool = False
    egress_controls_verified: bool = False
    telemetry_controls_verified: bool = False
    scope: str = "PRODUCTION"
    local_controls_verified: bool = False


class ApprovalProvider(Protocol):
    def current(self, source_id: str) -> SourceApproval | None: ...


@dataclass(frozen=True)
class GatewayPage:
    source_id: str
    data_mode: str
    page: object = field(repr=False)


class _CredentialTransport(httpx.AsyncBaseTransport):
    """Inject secrets below HTTPX request logging, keeping outer URLs clean."""

    def __init__(self, source_id: str, key: str, inner: httpx.AsyncBaseTransport,
                 expected_params: dict):
        self._source_id, self._key, self._inner = source_id, key, inner
        self._endpoint = source_capability(source_id).endpoint
        self._expected_params = dict(expected_params)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.method != "GET" or str(request.url) != self._endpoint
                or request.url.query or request.content):
            raise GatewayError("OUTBOUND_DESTINATION_REJECTED")
        params = request.extensions.get("trendscout_params")
        if (not isinstance(params, dict) or params != self._expected_params
                or any(not isinstance(k, str) for k in params)
                or any(not isinstance(v, (str, int)) or isinstance(v, bool) for v in params.values())
                or "api_key" in params or "key" in params):
            raise GatewayError("OUTBOUND_PARAMETERS_REJECTED")
        headers = dict(request.headers)
        if any(name.lower() in {"authorization", "x-goog-api-key", "proxy-authorization"}
               for name in headers):
            raise GatewayError("OUTBOUND_HEADERS_REJECTED")
        if self._source_id in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}:
            params = {**params, "api_key": self._key}
        else:
            headers["x-goog-api-key"] = self._key
        # Drop tracing hooks supplied on the outer request. Other process-wide or
        # network instrumentation still requires review before any live rollout.
        timeout = request.extensions.get("timeout", {})
        inner_request = httpx.Request("GET", request.url.copy_merge_params(params),
                                     headers=headers, extensions={"timeout": timeout})
        return await self._inner.handle_async_request(inner_request)

    async def aclose(self) -> None:
        # The gateway owns the shared underlying transport.
        return None


class LivePageGateway:
    """One source, one bounded page per fenced and reserved physical attempt."""

    def __init__(self, source_id: str, credentials: CredentialProvider,
                 approvals: ApprovalProvider, runner: GuardedAttemptRunner,
                 *, inner: httpx.AsyncBaseTransport, offline: bool,
                 clock=lambda: datetime.now(timezone.utc), policy: HttpPolicy | None = None,
                 _construction_token=None):
        if (source_id not in _SOURCES or not callable(getattr(credentials, "resolve", None))
                or not callable(getattr(approvals, "current", None))
                or not isinstance(runner, GuardedAttemptRunner) or not callable(clock)):
            raise ValueError("invalid source execution boundary")
        if type(offline) is not bool or (offline and not isinstance(inner, httpx.MockTransport)):
            raise ValueError("offline gateway requires MockTransport")
        if not offline and (type(inner) is not httpx.AsyncHTTPTransport
                            or _construction_token is not _NETWORK_CONSTRUCTION):
            raise ValueError("live gateway requires the controlled HTTP transport")
        self.source_id, self.credentials, self.approvals = source_id, credentials, approvals
        self.runner, self._inner, self._offline, self.clock = runner, inner, offline, clock
        self.policy = policy or HttpPolicy(max_retries=0)
        if not isinstance(self.policy, HttpPolicy) or self.policy.max_retries != 0:
            raise ValueError("gateway performs one attempt; retries belong to the worker")
        self._closed = False

    @classmethod
    def offline_fixture(cls, source_id, credentials, approvals, runner, *, transport, **kwargs):
        if not isinstance(transport, httpx.MockTransport):
            raise ValueError("offline gateway refuses real network transports")
        return cls(source_id, credentials, approvals, runner, inner=transport, offline=True, **kwargs)

    @classmethod
    async def controlled_network(cls, source_id, credentials, approvals, runner, **kwargs):
        """Construct an unwired transport; approval is checked before any attempt."""
        inner = httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)
        try:
            gateway = cls(source_id, credentials, approvals, runner, inner=inner, offline=False,
                          _construction_token=_NETWORK_CONSTRUCTION, **kwargs)
            gateway._check_approval()
            return gateway
        except BaseException:
            await inner.aclose()
            raise

    def _check_approval(self):
        return checked_source_approval(self.approvals, self.source_id,
            self.runner.admission.policy.plan_fingerprint, self.clock, runner=self.runner)

    def _credential(self):
        reference = source_capability(self.source_id).credential_reference
        try:
            value = self.credentials.resolve(reference)
        except Exception:
            raise GatewayError("CREDENTIAL_UNAVAILABLE") from None
        if (not isinstance(value, str) or not 8 <= len(value) <= 256
                or any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise GatewayError("CREDENTIAL_UNAVAILABLE")
        return value

    def _params(self, context, cursor, max_results):
        try:
            if self.source_id in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}:
                if not isinstance(context, SerpContext) or context.source_id != self.source_id:
                    raise ValueError("source mismatch")
                params = build_request_params(context, cursor)
                if not self._offline:
                    # Provider status "Success" does not prove a cache miss.
                    # Force a new search for live verification and charge it in
                    # the same guarded budget as every other physical attempt.
                    params["no_cache"] = "true"
            else:
                if not isinstance(context, YouTubeContext) or context.source_id != self.source_id:
                    raise ValueError("source mismatch")
                plan = get_query_plan(context.plan_id, context.plan_version)
                if context not in build_execution_plan(plan, context.batch_started_at).queries:
                    raise ValueError("query is not in the server plan")
                params = build_search_params(context, page_token=cursor, max_results=max_results)
            if context.plan_fingerprint != self.runner.admission.policy.plan_fingerprint:
                raise ValueError("plan mismatch")
            return params
        except Exception:
            raise GatewayError("INVALID_SERVER_QUERY") from None

    async def fetch_page(self, *, context, cursor, job_id, owner, fence, attempt_id,
                         max_results=50, timeout_seconds=15, on_page_receipt=None) -> GatewayPage:
        if self._closed:
            raise GatewayError("GATEWAY_CLOSED")
        if on_page_receipt is not None and not callable(on_page_receipt):
            raise GatewayError("INVALID_RECEIPT_CALLBACK")
        approved = self._check_approval()
        params = self._params(context, cursor, max_results)
        if (type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds
                <= self.policy.total_timeout_seconds):
            raise GatewayError("INVALID_TIMEOUT")
        key = self._credential()
        endpoint = source_capability(self.source_id).endpoint
        parsed_receipt = []

        async def parse_response(response):
            dummy = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _request: httpx.Response(500))
            )
            try:
                if self.source_id in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}:
                    parser = SerpApiClient(dummy, None, SharedQuotaPool(1, 1),
                                           self.policy, utcnow=self.clock)
                    page = parser.parse_response(response, context=context, cursor=cursor,
                                                 sensitive_key=key)
                else:
                    parser = YouTubeSearchClient(client=dummy, credential_resolver=None,
                                                 http_policy=self.policy, utcnow=self.clock)
                    page = parser.parse_response(response, context=context,
                                                 max_results=max_results)
            finally:
                await dummy.aclose()
            return GatewayPage(self.source_id, self.data_mode, page)

        async def before_settle(value):
            code, response = value
            if code != "RESPONSE":
                return
            try:
                receipt = await parse_response(response)
            except (SerpApiError, YouTubeSourceError) as exc:
                parsed_receipt.append((None, exc))
                return
            parsed_receipt.append((receipt, None))
            callback_result = await asyncio.to_thread(on_page_receipt, receipt)
            if inspect.isawaitable(callback_result):
                await callback_result

        async def request():
            try:
                if self._check_approval() != approved:
                    raise GatewayError("SOURCE_NOT_APPROVED")
            except GatewayError:
                return ("SOURCE_NOT_APPROVED", None)
            transport = _CredentialTransport(self.source_id, key, self._inner, params)
            # A per-attempt client closes its wrapper but the shared underlying
            # transport is closed only when the gateway itself is closed.
            async with httpx.AsyncClient(transport=transport, follow_redirects=False,
                                         trust_env=False) as client:
                async with asyncio.timeout(timeout_seconds):
                    async with client.stream("GET", endpoint, headers={"Accept": "application/json"},
                        extensions={"trendscout_params": params}, timeout=self.policy.request_timeout(),
                        follow_redirects=False) as response:
                        length = response.headers.get("Content-Length")
                        if length and length.isdigit() and int(length) > _MAX_RESPONSE_BYTES:
                            return ("RESPONSE_TOO_LARGE", None)
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                                return ("RESPONSE_TOO_LARGE", None)
                            content.extend(chunk)
                        raw = bytes(content).decode("utf-8", errors="replace")
                        try:
                            decoded = json.loads(raw)
                            if isinstance(decoded, dict):
                                exposed = (decoded.get("organic_results", [])
                                    if self.source_id in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}
                                    else {"items": decoded.get("items"),
                                          "nextPageToken": decoded.get("nextPageToken")})
                                decoded = json.dumps(exposed, ensure_ascii=False)
                            else:
                                decoded = ""
                        except (TypeError, ValueError, RecursionError):
                            decoded = ""
                        if _contains_secret(decoded, key):
                            return ("SENSITIVE_UPSTREAM_RESPONSE", None)
                        safe_response = httpx.Response(response.status_code, headers=response.headers,
                                                       content=bytes(content))
                        return ("RESPONSE", safe_response)

        outcome = await self.runner.run(job_id=job_id, owner=owner, fence=fence,
            source_id=self.source_id, attempt_id=attempt_id, timeout_seconds=timeout_seconds,
            request=request,
            before_settle=before_settle if on_page_receipt is not None else None)
        if outcome.status != "COMPLETED":
            raise GatewayError(outcome.status)
        if outcome.settlement_pending and not parsed_receipt:
            raise GatewayError("SETTLEMENT_PENDING")
        code, response = outcome.value
        if code != "RESPONSE":
            raise GatewayError(code)
        if parsed_receipt:
            receipt, error = parsed_receipt[0]
            if error is not None:
                raise error
            return receipt
        return await parse_response(response)

    async def aclose(self):
        if not self._closed:
            self._closed = True
            await self._inner.aclose()

    @property
    def data_mode(self) -> str:
        return "SYNTHETIC" if self._offline else "LIVE"
