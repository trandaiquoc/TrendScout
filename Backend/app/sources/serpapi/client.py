"""Fixed SerpApi request shape and strictly offline HTTP transport."""

import json
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import unquote

import httpx

from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, source_capability
from app.sources.http_policy import HttpDeadlineExceeded, HttpPolicy, HttpResponseTooLarge
from app.sources.serpapi.errors import SerpApiError
from app.sources.serpapi.models import ProviderPage, QueryContext, ScopedOffset
from app.sources.serpapi.pagination import next_offset, validate_context, validate_offset
from app.sources.serpapi.quota import SharedQuotaPool


class CredentialResolver(Protocol):
    def resolve(self, reference: str) -> str | None: ...


def build_request_params(context: QueryContext, cursor: ScopedOffset) -> dict[str, str | int]:
    try:
        validate_context(context)
        offset = validate_offset(context, cursor)
    except ValueError:
        raise SerpApiError("INVALID_CONFIGURATION") from None
    if context.source_id == GOOGLE_SOURCE_ID:
        return {"engine": "google", "q": context.canonical_query, "gl": "us", "hl": "en",
                "google_domain": "google.com", "start": offset, "output": "json"}
    if context.source_id == BING_SOURCE_ID:
        return {"engine": "bing", "q": context.canonical_query, "mkt": "en-US",
                "first": offset, "output": "json"}
    raise SerpApiError("INVALID_CONFIGURATION")


def create_offline_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    if not isinstance(transport, httpx.MockTransport):
        raise TypeError("D4 requires httpx.MockTransport")
    return httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)


class SerpApiClient:
    """One attempt per call; caller owns bounded retries and deadline."""

    def __init__(self, client: httpx.AsyncClient, resolver: CredentialResolver | None,
                 pool: SharedQuotaPool, policy: HttpPolicy | None = None):
        if (not isinstance(client, httpx.AsyncClient)
                or not isinstance(getattr(client, "_transport", None), httpx.MockTransport)):
            raise ValueError("D4 refuses live HTTP transports")
        if not isinstance(pool, SharedQuotaPool):
            raise TypeError("A shared provider quota pool is required")
        if policy is not None and not isinstance(policy, HttpPolicy):
            raise TypeError("A validated HTTP policy is required")
        self.client = client
        self.resolver = resolver
        self.pool = pool
        self.policy = policy or HttpPolicy()

    def _credential(self, reference: str | None) -> str:
        try:
            key = self.resolver.resolve(reference) if self.resolver and reference else None
        except Exception:
            raise SerpApiError("INVALID_CONFIGURATION") from None
        if key is None:
            raise SerpApiError("CREDENTIAL_NOT_CONFIGURED")
        if (not isinstance(key, str) or not key or len(key) > 256
                or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise SerpApiError("INVALID_CONFIGURATION")
        return key

    async def search(self, context: QueryContext, cursor: ScopedOffset, *,
                     timeout_seconds: float | None = None) -> ProviderPage:
        params = build_request_params(context, cursor)
        key = self._credential(source_capability(context.source_id).credential_reference)
        if not self.pool.reserve():
            raise SerpApiError("REQUEST_BUDGET_EXHAUSTED")
        # SerpApi documents api_key as a parameter. Never expose request URLs or bodies.
        params["api_key"] = key
        endpoint = source_capability(context.source_id).endpoint
        try:
            response = await self.policy.get_bounded(
                self.client, endpoint, headers={"Accept": "application/json"}, params=params,
                max_response_bytes=1_000_000, total_timeout_seconds=timeout_seconds,
            )
        except HttpResponseTooLarge:
            raise SerpApiError("RESPONSE_TOO_LARGE") from None
        except (HttpDeadlineExceeded, httpx.TimeoutException, TimeoutError):
            raise SerpApiError("UPSTREAM_TIMEOUT", retryable=True) from None
        except httpx.RequestError:
            raise SerpApiError("NETWORK_ERROR", retryable=True) from None
        status = response.status_code
        try:
            payload = json.loads(response.content.decode("utf-8", errors="strict"))
        except (UnicodeError, ValueError, RecursionError):
            payload = None
        if not 200 <= status < 300:
            self._raise_http_error(status, response, payload)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
        if content_type != "application/json" or not isinstance(payload, dict):
            raise SerpApiError("MALFORMED_RESPONSE", status=status)
        metadata = payload.get("search_metadata")
        if (not isinstance(metadata, dict) or not isinstance(metadata.get("status"), str)
                or metadata.get("status") not in {"Success", "Cached"}):
            raise SerpApiError("PROVIDER_ERROR", status=status)
        items = payload.get("organic_results")
        if items is None:
            info = payload.get("search_information")
            state = info.get("organic_results_state") if isinstance(info, dict) else None
            if not isinstance(state, str) or state.lower() != "fully empty":
                raise SerpApiError("MALFORMED_RESPONSE", status=status)
            items = []
        if not isinstance(items, list):
            raise SerpApiError("MALFORMED_RESPONSE", status=status)
        if len(items) > 100:
            raise SerpApiError("PROVIDER_RESULT_LIMIT", status=status)
        if items and payload.get("error") is not None:
            raise SerpApiError("PROVIDER_ERROR", status=status)
        if key in unquote(json.dumps(items, ensure_ascii=False)):
            raise SerpApiError("MALFORMED_RESPONSE", status=status)
        pagination = payload.get("serpapi_pagination")
        if pagination is not None and not isinstance(pagination, dict):
            raise SerpApiError("MALFORMED_RESPONSE", status=status)
        pagination_error = None
        try:
            cursor_next = next_offset(context, cursor, pagination.get("next") if pagination else None)
        except ValueError:
            cursor_next = None
            pagination_error = "INVALID_PAGE_OFFSET"
        cache = "CACHED" if metadata.get("status") == "Cached" else "UNKNOWN"
        created = None
        raw_created = metadata.get("created_at")
        if isinstance(raw_created, str) and len(raw_created) <= 64:
            try:
                created = datetime.strptime(raw_created, "%Y-%m-%d %H:%M:%S UTC").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
        return ProviderPage(tuple(items), cursor_next, status, cache, created, pagination_error)

    def _raise_http_error(self, status: int, response: httpx.Response, payload: object) -> None:
        if 300 <= status < 400:
            raise SerpApiError("UNEXPECTED_REDIRECT", status=status)
        message = payload.get("error") if isinstance(payload, dict) else None
        if status == 429 and isinstance(message, str) and "run out of searches" in message.lower():
            raise SerpApiError("QUOTA_EXHAUSTED", status=status)
        if status == 429:
            raw = response.headers.get("Retry-After")
            delay = self.policy.retry_delay(raw, 0, datetime.now(timezone.utc)) if raw else None
            # A Retry-After longer than the configured cap must never be shortened.
            retryable = not raw or delay is not None
            raise SerpApiError("RATE_LIMITED", status=status, retryable=retryable, retry_after=delay)
        code = {400: "INVALID_QUERY", 401: "UNAUTHORIZED", 403: "PERMISSION_DENIED",
                408: "UPSTREAM_TIMEOUT", 504: "UPSTREAM_TIMEOUT"}.get(
            status, "TRANSIENT_UPSTREAM_ERROR" if status >= 500 else "PROVIDER_ERROR"
        )
        raise SerpApiError(code, status=status, retryable=status == 408 or status >= 500)
