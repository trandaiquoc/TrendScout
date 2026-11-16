"""Redacted SerpApi Account API contract for conservative local quota checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

from app.sources.http_policy import HttpPolicy
from app.sources.serpapi.errors import SerpApiError

ACCOUNT_ENDPOINT = "https://serpapi.com/account.json"
_MAX_ACCOUNT_BYTES = 32_000
_NETWORK_CONSTRUCTION = object()


def _count(payload: dict, name: str, *, optional: bool = False) -> int | None:
    value = payload.get(name)
    if value is None and optional:
        return None
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        raise ValueError("invalid account quota field")
    return value


@dataclass(frozen=True)
class AccountQuotaSnapshot:
    observed_at: datetime
    monthly_limit: int | None
    monthly_remaining: int | None
    total_remaining: int
    month_to_date_searches: int
    current_hour_searches: int
    hourly_limit: int
    plan_renewal_date: str | None = None

    def public_metadata(self) -> dict:
        """Expose numeric quota metadata only; omit account identifiers and raw JSON."""
        return {
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
            "monthly_limit": self.monthly_limit,
            "monthly_remaining": self.monthly_remaining,
            "total_remaining": self.total_remaining,
            "month_to_date_searches": self.month_to_date_searches,
            "current_hour_searches": self.current_hour_searches,
            "hourly_limit": self.hourly_limit,
            "plan_renewal_date": self.plan_renewal_date,
        }


def parse_account_response(response: httpx.Response, *, observed_at: datetime) -> AccountQuotaSnapshot:
    if (not isinstance(response, httpx.Response)
            or not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None or observed_at.utcoffset() is None):
        raise SerpApiError("ACCOUNT_RESPONSE_INVALID")
    if response.status_code == 429:
        delay = HttpPolicy().parse_retry_after(
            response.headers.get("Retry-After"), 0, observed_at
        )
        raise SerpApiError("ACCOUNT_RATE_LIMITED", status=429, retryable=True,
                           retry_after=delay)
    if response.status_code != 200:
        raise SerpApiError("ACCOUNT_API_UNAVAILABLE", status=response.status_code)
    if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise SerpApiError("ACCOUNT_RESPONSE_INVALID", status=response.status_code)
    try:
        raw = response.content
        if not raw or len(raw) > _MAX_ACCOUNT_BYTES:
            raise ValueError("account body is outside its size limit")
        payload = json.loads(raw.decode("utf-8", errors="strict"))
        if not isinstance(payload, dict) or payload.get("account_status") != "Active":
            raise ValueError("account is not active")
        monthly_limit = _count(payload, "searches_per_month", optional=True)
        plan_left = _count(payload, "plan_searches_left", optional=True)
        total_left = _count(payload, "total_searches_left")
        month_usage = _count(payload, "this_month_usage")
        this_hour = _count(payload, "this_hour_searches")
        hourly_limit = _count(payload, "account_rate_limit_per_hour")
        renewal = payload.get("plan_renewal_date")
        if renewal is not None:
            if not isinstance(renewal, str) or len(renewal) != 10:
                raise ValueError("invalid account renewal date")
            renewal_date = date.fromisoformat(renewal)
            if renewal_date.isoformat() != renewal:
                raise ValueError("invalid account renewal date")
        else:
            renewal = None
        if total_left is None or month_usage is None or this_hour is None or hourly_limit is None:
            raise ValueError("account quota fields are incomplete")
        monthly_remaining = min(plan_left, total_left) if plan_left is not None else None
        if monthly_remaining is not None and monthly_limit is not None:
            monthly_remaining = min(monthly_remaining, monthly_limit)
        return AccountQuotaSnapshot(
            observed_at=observed_at.astimezone(timezone.utc),
            monthly_limit=monthly_limit,
            monthly_remaining=monthly_remaining,
            total_remaining=total_left,
            month_to_date_searches=month_usage,
            current_hour_searches=this_hour,
            hourly_limit=hourly_limit,
            plan_renewal_date=renewal,
        )
    except SerpApiError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise SerpApiError("ACCOUNT_RESPONSE_INVALID", status=response.status_code) from None


class _AccountCredentialTransport(httpx.AsyncBaseTransport):
    """Inject api_key only inside the transport so outer request logs stay clean."""

    def __init__(self, key: str, inner: httpx.AsyncBaseTransport):
        self.key, self.inner = key, inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.method != "GET" or str(request.url) != ACCOUNT_ENDPOINT
                or request.content or request.url.query
                or request.headers.get("authorization") is not None):
            raise SerpApiError("ACCOUNT_REQUEST_REJECTED")
        timeout = request.extensions.get("timeout", {})
        inner_request = httpx.Request(
            "GET", request.url.copy_merge_params({"api_key": self.key}),
            headers={"accept": "application/json"},
            extensions={"timeout": timeout},
        )
        return await self.inner.handle_async_request(inner_request)

    async def aclose(self) -> None:
        return None


class SerpApiAccountClient:
    """One bounded account read; construction never performs a network request."""

    def __init__(self, key: str, *, inner: httpx.AsyncBaseTransport,
                 offline: bool, clock=lambda: datetime.now(timezone.utc),
                 _construction_token=None):
        if (not isinstance(key, str) or not 8 <= len(key) <= 256
                or any(ord(char) < 33 or ord(char) > 126 for char in key)
                or type(offline) is not bool or not callable(clock)
                or offline and not isinstance(inner, httpx.MockTransport)
                or not offline and (type(inner) is not httpx.AsyncHTTPTransport
                    or _construction_token is not _NETWORK_CONSTRUCTION)):
            raise ValueError("validated credential and controlled Account API transport required")
        self._key, self._inner, self.offline, self.clock = key, inner, offline, clock
        self._transport = _AccountCredentialTransport(key, inner)
        self._client = httpx.AsyncClient(
            transport=self._transport, trust_env=False, follow_redirects=False
        )
        self._policy = HttpPolicy(max_retries=0)
        self._closed = False

    @classmethod
    def offline_fixture(cls, key, *, transport, **kwargs):
        if not isinstance(transport, httpx.MockTransport):
            raise ValueError("Account API offline fixture requires MockTransport")
        return cls(key, inner=transport, offline=True, **kwargs)

    @classmethod
    async def controlled_network(cls, key, **kwargs):
        inner = httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)
        try:
            return cls(key, inner=inner, offline=False,
                       _construction_token=_NETWORK_CONSTRUCTION, **kwargs)
        except BaseException:
            await inner.aclose()
            raise

    async def fetch(self) -> AccountQuotaSnapshot:
        if self._closed:
            raise SerpApiError("ACCOUNT_CLIENT_CLOSED")
        try:
            response = await self._policy.get_bounded(
                self._client, ACCOUNT_ENDPOINT,
                headers={"Accept": "application/json"},
                max_response_bytes=_MAX_ACCOUNT_BYTES,
            )
            return parse_account_response(
                response, observed_at=self.clock()
            )
        except SerpApiError:
            raise
        except Exception:
            # Never retain HTTPX request URLs, provider bodies, or credential text.
            raise SerpApiError("ACCOUNT_API_UNAVAILABLE") from None

    async def aclose(self):
        if not self._closed:
            self._closed = True
            await self._client.aclose()
            await self._inner.aclose()
