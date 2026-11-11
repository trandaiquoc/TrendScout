"""Fixed-host YouTube search request builder and bounded REST client."""

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone

import httpx

from app.ports.credentials import CredentialProvider
from app.sources.catalog import YOUTUBE_SOURCE_ID, source_capability
from app.sources.http_policy import (
    HttpDeadlineExceeded,
    HttpPolicy,
    HttpResponseTooLarge,
)
from app.sources.youtube.errors import YouTubeErrorCode, YouTubeSourceError
from app.sources.youtube.models import ProviderPage, QueryContext, ScopedPageToken, format_utc

YOUTUBE_SEARCH_ENDPOINT = source_capability(YOUTUBE_SOURCE_ID).endpoint
YOUTUBE_API_KEY_REFERENCE = "YOUTUBE_API_KEY"
MAX_RESPONSE_BYTES = 1_000_000
YOUTUBE_API_KEY_HEADER = "X-Goog-Api-Key"
_PAGE_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{1,2048}$")


def build_search_params(
    context: QueryContext,
    *,
    page_token: ScopedPageToken | None,
    max_results: int,
) -> dict[str, str | int]:
    """Build the fixed search.list allowlist from a server-owned query context."""
    if context.source_id != YOUTUBE_SOURCE_ID or context.provider_id != "youtube_data_api_v3":
        raise YouTubeSourceError(YouTubeErrorCode.INVALID_CONFIGURATION)
    if context.region_code != "US" or context.relevance_language != "en":
        raise YouTubeSourceError(YouTubeErrorCode.INVALID_CONFIGURATION)
    if not context.canonical_query or len(context.canonical_query) > 256:
        raise YouTubeSourceError(
            YouTubeErrorCode.INVALID_QUERY, query_scope=context.taxonomy_scope_label
        )
    if type(max_results) is not int or not 1 <= max_results <= 50:
        raise YouTubeSourceError(YouTubeErrorCode.INVALID_CONFIGURATION)
    params: dict[str, str | int] = {
        "part": "snippet",
        "type": "video",
        "q": context.canonical_query,
        "regionCode": "US",
        "relevanceLanguage": "en",
        "publishedAfter": format_utc(context.published_after),
        "maxResults": max_results,
        "order": "relevance",
    }
    if page_token is not None:
        if page_token.context_key != context.context_key:
            raise YouTubeSourceError(
                YouTubeErrorCode.INVALID_PAGE_TOKEN,
                query_scope=context.taxonomy_scope_label,
                stop_reason="CURSOR_CONTEXT_MISMATCH",
            )
        if not isinstance(page_token.value, str) or not _PAGE_TOKEN.fullmatch(page_token.value):
            raise YouTubeSourceError(
                YouTubeErrorCode.INVALID_PAGE_TOKEN,
                query_scope=context.taxonomy_scope_label,
            )
        params["pageToken"] = page_token.value
    return params


def create_offline_youtube_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    """Create the only HTTP client permitted for this offline implementation."""
    if not isinstance(transport, httpx.MockTransport):
        raise TypeError("D3B YouTube transport must be httpx.MockTransport")
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        verify=True,
    )


class YouTubeSearchClient:
    """One provider attempt per call; retry decisions belong to the batch budget."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        credential_resolver: CredentialProvider | None,
        http_policy: HttpPolicy,
        utcnow=lambda: datetime.now(timezone.utc),
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        if not isinstance(client, httpx.AsyncClient):
            raise TypeError("YouTube search requires an injected AsyncClient")
        if not isinstance(getattr(client, "_transport", None), httpx.MockTransport):
            raise ValueError("D3B YouTube search refuses live HTTP transports")
        if not isinstance(http_policy, HttpPolicy):
            raise TypeError("YouTube search requires a shared validated HttpPolicy")
        if type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 10_000_000:
            raise ValueError("YouTube response limit must be within 1024..10000000 bytes")
        self.client = client
        self.credential_resolver = credential_resolver
        self.http_policy = http_policy
        self.utcnow = utcnow
        self.max_response_bytes = max_response_bytes

    def resolve_credential(self, *, query_scope: str | None = None) -> str:
        if self.credential_resolver is None:
            raise YouTubeSourceError(
                YouTubeErrorCode.CREDENTIAL_NOT_CONFIGURED,
                query_scope=query_scope,
            )
        try:
            api_key = self.credential_resolver.resolve(YOUTUBE_API_KEY_REFERENCE)
        except Exception:
            raise YouTubeSourceError(
                YouTubeErrorCode.INVALID_CONFIGURATION,
                query_scope=query_scope,
            ) from None
        if api_key is None:
            raise YouTubeSourceError(
                YouTubeErrorCode.CREDENTIAL_NOT_CONFIGURED,
                query_scope=query_scope,
            )
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or len(api_key) > 256
            or any(ord(character) < 0x21 for character in api_key)
        ):
            raise YouTubeSourceError(
                YouTubeErrorCode.INVALID_CONFIGURATION,
                query_scope=query_scope,
            )
        return api_key

    async def search(
        self,
        context: QueryContext,
        page_token: ScopedPageToken | None,
        *,
        api_key: str,
        max_results: int,
        timeout_seconds: float | None = None,
    ) -> ProviderPage:
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or len(api_key) > 256
            or any(ord(character) < 0x21 for character in api_key)
        ):
            raise YouTubeSourceError(
                YouTubeErrorCode.INVALID_CONFIGURATION,
                query_scope=context.taxonomy_scope_label,
            )
        try:
            params = build_search_params(
                context,
                page_token=page_token,
                max_results=max_results,
            )
        except YouTubeSourceError:
            raise
        try:
            response = await self.http_policy.get_bounded(
                self.client,
                YOUTUBE_SEARCH_ENDPOINT,
                headers={"Accept": "application/json", YOUTUBE_API_KEY_HEADER: api_key},
                params=params,
                max_response_bytes=self.max_response_bytes,
                total_timeout_seconds=timeout_seconds,
            )
        except HttpDeadlineExceeded:
            raise
        except HttpResponseTooLarge as exc:
            raise YouTubeSourceError(
                YouTubeErrorCode.RESPONSE_TOO_LARGE,
                http_status=exc.status_code,
                query_scope=context.taxonomy_scope_label,
            ) from None
        except (httpx.TimeoutException, TimeoutError):
            raise
        except httpx.RequestError:
            raise

        status = response.status_code
        if 300 <= status < 400:
            raise YouTubeSourceError(
                YouTubeErrorCode.UNEXPECTED_REDIRECT,
                http_status=status,
                query_scope=context.taxonomy_scope_label,
            )
        if not 200 <= status < 300:
            raise self._http_error(response, context)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise YouTubeSourceError(
                YouTubeErrorCode.MALFORMED_RESPONSE,
                http_status=status,
                request_completed=True,
                query_scope=context.taxonomy_scope_label,
            )
        try:
            payload = json.loads(response.content.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise YouTubeSourceError(
                YouTubeErrorCode.MALFORMED_RESPONSE,
                http_status=status,
                request_completed=True,
                query_scope=context.taxonomy_scope_label,
            ) from None
        if not isinstance(payload, dict) or payload.get("kind") != "youtube#searchListResponse":
            raise YouTubeSourceError(
                YouTubeErrorCode.MALFORMED_RESPONSE,
                http_status=status,
                request_completed=True,
                query_scope=context.taxonomy_scope_label,
            )
        items = payload.get("items")
        if not isinstance(items, list) or len(items) > max_results:
            raise YouTubeSourceError(
                YouTubeErrorCode.MALFORMED_RESPONSE,
                http_status=status,
                request_completed=True,
                query_scope=context.taxonomy_scope_label,
            )
        next_page_token = payload.get("nextPageToken")
        if next_page_token is not None and (
            not isinstance(next_page_token, str) or not _PAGE_TOKEN.fullmatch(next_page_token)
        ):
            raise YouTubeSourceError(
                YouTubeErrorCode.INVALID_PAGE_TOKEN,
                http_status=status,
                request_completed=True,
                query_scope=context.taxonomy_scope_label,
            )
        page_info = payload.get("pageInfo")
        estimated_total = None
        if isinstance(page_info, dict):
            total = page_info.get("totalResults")
            if type(total) is int and 0 <= total <= 1_000_000:
                estimated_total = total
        return ProviderPage(
            items=tuple(items),
            next_page_token=next_page_token,
            estimated_total_results=estimated_total,
            response_status=status,
        )

    def _http_error(self, response: httpx.Response, context: QueryContext) -> YouTubeSourceError:
        status = response.status_code
        reason = _provider_reason(response.content)
        if reason in {"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}:
            code, retryable = YouTubeErrorCode.QUOTA_EXHAUSTED, False
        elif reason in {"invalidPageToken", "invalidPageTokenParameter"}:
            code, retryable = YouTubeErrorCode.INVALID_PAGE_TOKEN, False
        elif reason in {"invalidQuery", "invalidSearchFilter", "invalidParameter"}:
            code, retryable = YouTubeErrorCode.INVALID_QUERY, False
        elif reason in {"keyInvalid", "authError", "invalidCredentials"} or status == 401:
            code, retryable = YouTubeErrorCode.UNAUTHORIZED, False
        elif status == 429:
            code, retryable = YouTubeErrorCode.RATE_LIMITED, True
        elif status in {408, 504}:
            code, retryable = YouTubeErrorCode.UPSTREAM_TIMEOUT, True
        elif 500 <= status <= 599:
            code, retryable = YouTubeErrorCode.TRANSIENT_UPSTREAM_ERROR, True
        elif status == 400:
            code, retryable = YouTubeErrorCode.INVALID_QUERY, False
        else:
            code, retryable = YouTubeErrorCode.PERMISSION_DENIED, False

        retry_after_seconds = None
        retry_after_exceeds_limit = False
        retry_after_header = response.headers.get("Retry-After")
        if retry_after_header:
            retry_after_seconds = self.http_policy.retry_delay(
                retry_after_header,
                0,
                self.utcnow().astimezone(timezone.utc),
            )
            retry_after_exceeds_limit = retry_after_seconds is None
        return YouTubeSourceError(
            code,
            http_status=status,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
            retry_after_exceeds_limit=retry_after_exceeds_limit,
            query_scope=context.taxonomy_scope_label,
        )


def _provider_reason(content: bytes) -> str | None:
    try:
        payload = json.loads(content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    errors = error.get("errors")
    if not isinstance(errors, list):
        return None
    for item in errors:
        if isinstance(item, Mapping):
            reason = item.get("reason")
            if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", reason):
                return reason
    return None
