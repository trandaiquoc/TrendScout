"""Shared bounded HTTP request and retry policy for source adapters."""

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping

import httpx


class HttpDeadlineExceeded(TimeoutError):
    """A request exceeded the policy deadline rather than raising its own timeout."""


class HttpResponseTooLarge(ValueError):
    """A streamed HTTP response exceeded the caller's byte limit."""

    def __init__(self, status_code: int | None = None):
        self.status_code = status_code
        super().__init__("HTTP response exceeded configured size limit")


@dataclass(frozen=True)
class HttpPolicy:
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 15.0
    total_timeout_seconds: float = 15.0
    max_retries: int = 2
    max_retry_wait_seconds: float = 30.0

    def __post_init__(self):
        timeouts = (
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.total_timeout_seconds,
        )
        if any(
            type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or value > 60
            for value in timeouts
        ):
            raise ValueError("HTTP timeouts must be finite and within (0, 60] seconds")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 5:
            raise ValueError("HTTP max retries must be between 0 and 5")
        if (
            type(self.max_retry_wait_seconds) not in (int, float)
            or not math.isfinite(self.max_retry_wait_seconds)
            or not 0 <= self.max_retry_wait_seconds <= 300
        ):
            raise ValueError("HTTP retry wait must be within [0, 300] seconds")

    def request_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=self.read_timeout_seconds,
            pool=self.connect_timeout_seconds,
        )

    async def get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        """Perform one GET with explicit connect/read timeouts and a total deadline."""
        deadline = asyncio.timeout(self.total_timeout_seconds)
        try:
            async with deadline:
                return await client.get(url, headers=headers, timeout=self.request_timeout())
        except TimeoutError as exc:
            if deadline.expired():
                raise HttpDeadlineExceeded("HTTP request exceeded total deadline") from exc
            raise

    async def get_bounded(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str | int] | None = None,
        max_response_bytes: int,
        total_timeout_seconds: float | None = None,
    ) -> httpx.Response:
        """Stream a GET under shared timeouts and reject oversized bodies before buffering."""
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 10_000_000:
            raise ValueError("HTTP response size limit must be between 1 and 10000000 bytes")
        timeout_seconds = (
            self.total_timeout_seconds
            if total_timeout_seconds is None
            else total_timeout_seconds
        )
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > self.total_timeout_seconds
        ):
            raise ValueError("HTTP request deadline must be within the configured total timeout")
        deadline = asyncio.timeout(timeout_seconds)
        response_status = None
        try:
            async with deadline:
                async with client.stream(
                    "GET",
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.request_timeout(),
                    follow_redirects=False,
                ) as response:
                    response_status = response.status_code
                    content_length = response.headers.get("Content-Length")
                    if content_length and content_length.isdigit() and int(content_length) > max_response_bytes:
                        raise HttpResponseTooLarge(response_status)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > max_response_bytes:
                            raise HttpResponseTooLarge(response_status)
                        body.extend(chunk)
                    return httpx.Response(
                        status_code=response.status_code,
                        headers=response.headers,
                        content=bytes(body),
                        request=response.request,
                        extensions=response.extensions,
                    )
        except TimeoutError as exc:
            if deadline.expired():
                raise HttpDeadlineExceeded("HTTP request exceeded total deadline") from exc
            raise

    @staticmethod
    def is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code <= 599

    def retry_delay(
        self,
        retry_after: str | None,
        retry_index: int,
        now: datetime | None = None,
    ) -> float | None:
        """Return bounded Retry-After/backoff seconds, or None above the wait cap."""
        delay = self.parse_retry_after(retry_after, retry_index, now)
        return delay if delay <= self.max_retry_wait_seconds else None

    @staticmethod
    def parse_retry_after(
        retry_after: str | None,
        retry_index: int,
        now: datetime | None = None,
    ) -> float:
        """Parse Retry-After without a provider-specific cap for legacy callers."""
        if retry_after:
            try:
                seconds = float(retry_after)
                if math.isfinite(seconds):
                    return max(0.0, seconds)
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(retry_after)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    current = now or datetime.now(timezone.utc)
                    return max(0.0, (parsed - current).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(2 ** max(0, retry_index))

    def backoff_delay(self, retry_index: int) -> float:
        """Return a capped exponential delay for retryable transport exceptions."""
        return min(float(2 ** max(0, retry_index)), self.max_retry_wait_seconds)
