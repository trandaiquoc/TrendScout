"""Stable, credential-safe errors for the YouTube search source."""

from enum import Enum


class YouTubeErrorCode(str, Enum):
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    CREDENTIAL_NOT_CONFIGURED = "CREDENTIAL_NOT_CONFIGURED"
    UNAUTHORIZED = "UNAUTHORIZED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_QUERY = "INVALID_QUERY"
    INVALID_PAGE_TOKEN = "INVALID_PAGE_TOKEN"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT_UPSTREAM_ERROR = "TRANSIENT_UPSTREAM_ERROR"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    REQUEST_BUDGET_EXHAUSTED = "REQUEST_BUDGET_EXHAUSTED"
    RETRY_BUDGET_EXHAUSTED = "RETRY_BUDGET_EXHAUSTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CANCELLED = "CANCELLED"
    PARTIAL_BATCH = "PARTIAL_BATCH"
    UNEXPECTED_REDIRECT = "UNEXPECTED_REDIRECT"


_SAFE_MESSAGES = {
    YouTubeErrorCode.INVALID_CONFIGURATION: "The YouTube source configuration is invalid.",
    YouTubeErrorCode.CREDENTIAL_NOT_CONFIGURED: "The YouTube source is not configured.",
    YouTubeErrorCode.UNAUTHORIZED: "The YouTube API did not authorize this request.",
    YouTubeErrorCode.PERMISSION_DENIED: "The YouTube API denied this request.",
    YouTubeErrorCode.INVALID_QUERY: "The configured YouTube query was rejected.",
    YouTubeErrorCode.INVALID_PAGE_TOKEN: "The YouTube page cursor is invalid.",
    YouTubeErrorCode.QUOTA_EXHAUSTED: "The YouTube search quota is exhausted.",
    YouTubeErrorCode.RATE_LIMITED: "The YouTube API is rate limiting requests.",
    YouTubeErrorCode.TRANSIENT_UPSTREAM_ERROR: "The YouTube API returned a temporary error.",
    YouTubeErrorCode.UPSTREAM_TIMEOUT: "The YouTube API request timed out.",
    YouTubeErrorCode.NETWORK_ERROR: "The YouTube API request could not be completed.",
    YouTubeErrorCode.MALFORMED_RESPONSE: "The YouTube API response was invalid.",
    YouTubeErrorCode.RESPONSE_TOO_LARGE: "The YouTube API response exceeded the size limit.",
    YouTubeErrorCode.REQUEST_BUDGET_EXHAUSTED: "The configured request budget is exhausted.",
    YouTubeErrorCode.RETRY_BUDGET_EXHAUSTED: "The configured request budget cannot fund another retry.",
    YouTubeErrorCode.DEADLINE_EXCEEDED: "The YouTube query batch exceeded its deadline.",
    YouTubeErrorCode.CANCELLED: "The YouTube query batch was cancelled.",
    YouTubeErrorCode.PARTIAL_BATCH: "The YouTube query batch could not be completed.",
    YouTubeErrorCode.UNEXPECTED_REDIRECT: "The YouTube API returned an unexpected redirect.",
}


class YouTubeSourceError(Exception):
    """An error containing only stable, bounded metadata safe for reporting."""

    def __init__(
        self,
        code: YouTubeErrorCode,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        attempt_count: int = 0,
        retry_after_seconds: float | None = None,
        retry_after_exceeds_limit: bool = False,
        retry_after_unbounded_seconds: float | None = None,
        source_id: str = "youtube_video_search",
        provider_id: str = "youtube_data_api_v3",
        query_scope: str | None = None,
        stop_reason: str | None = None,
        request_completed: bool = False,
        cause_code: YouTubeErrorCode | None = None,
    ):
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.attempt_count = attempt_count
        self.retry_after_seconds = retry_after_seconds
        self.retry_after_exceeds_limit = retry_after_exceeds_limit
        self.retry_after_unbounded_seconds = retry_after_unbounded_seconds
        self.source_id = source_id
        self.provider_id = provider_id
        self.query_scope = query_scope
        self.stop_reason = stop_reason
        self.request_completed = request_completed
        self.cause_code = cause_code
        self.safe_message = _SAFE_MESSAGES[code]
        super().__init__(self.safe_message)

    def __repr__(self) -> str:
        return (
            "YouTubeSourceError("
            f"code={self.code.value!r}, http_status={self.http_status!r}, "
            f"retryable={self.retryable!r}, attempt_count={self.attempt_count!r}, "
            f"query_scope={self.query_scope!r}, stop_reason={self.stop_reason!r})"
        )
