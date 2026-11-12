"""Shared HTTP policy bounds timeouts and transient retries offline."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx

from app.sources.http_policy import (
    HttpDeadlineExceeded,
    HttpPolicy,
    HttpRequestFailure,
    HttpResponseTooLarge,
)


class HttpPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_timeouts_retries_and_bounds_are_validated(self):
        policy = HttpPolicy(
            connect_timeout_seconds=2,
            read_timeout_seconds=5,
            total_timeout_seconds=8,
            max_retries=3,
            max_retry_wait_seconds=12,
        )
        timeout = policy.request_timeout()

        self.assertEqual(timeout.connect, 2)
        self.assertEqual(timeout.read, 5)
        self.assertEqual(policy.max_retries, 3)
        self.assertTrue(policy.is_retryable_status(429))
        self.assertTrue(policy.is_retryable_status(503))
        self.assertFalse(policy.is_retryable_status(403))
        self.assertEqual(policy.retry_delay(None, 0, datetime.now(timezone.utc)), 1.0)
        with self.assertRaises(ValueError):
            HttpPolicy(total_timeout_seconds=0)
        with self.assertRaises(ValueError):
            HttpPolicy(max_retries=6)

    def test_retry_after_seconds_http_date_and_too_long_are_bounded(self):
        policy = HttpPolicy(max_retry_wait_seconds=10)
        now = datetime(2026, 11, 11, tzinfo=timezone.utc)

        self.assertEqual(policy.retry_delay("2.5", 0, now), 2.5)
        future = (now + timedelta(seconds=4)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertEqual(policy.retry_delay(future, 0, now), 4.0)
        too_long = (now + timedelta(seconds=11)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertIsNone(policy.retry_delay(too_long, 0, now))
        self.assertEqual(HttpPolicy.parse_retry_after("3601", 0, now), 3601.0)
        self.assertIsNone(HttpPolicy(max_retry_wait_seconds=300).retry_delay("3601", 0, now))
        self.assertEqual(policy.retry_delay("invalid", 2, now), 4.0)
        no_wait = HttpPolicy(max_retry_wait_seconds=0)
        self.assertIsNone(no_wait.retry_delay(None, 0, now))
        self.assertEqual(no_wait.backoff_delay(0), 0.0)

    async def test_request_timeout_is_total_and_cancellation_is_not_swallowed(self):
        policy = HttpPolicy(total_timeout_seconds=0.01, max_retries=0)
        started = asyncio.Event()

        class HangingClient:
            async def get(self, *_args, **_kwargs):
                started.set()
                await asyncio.Event().wait()

        with self.assertRaises(HttpDeadlineExceeded):
            await policy.get(HangingClient(), "https://example.invalid", headers={})
        self.assertTrue(started.is_set())

        class TimeoutClient:
            async def get(self, *_args, **_kwargs):
                raise TimeoutError("transport supplied timeout")

        with self.assertRaisesRegex(TimeoutError, "transport supplied timeout"):
            await policy.get(TimeoutClient(), "https://example.invalid", headers={})

        started.clear()
        task = asyncio.create_task(
            policy.get(HangingClient(), "https://example.invalid", headers={})
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_client_receives_finite_timeout_values(self):
        policy = HttpPolicy(
            connect_timeout_seconds=1, read_timeout_seconds=2, total_timeout_seconds=3
        )
        client = AsyncMock()
        client.get.return_value = httpx.Response(200)

        await policy.get(client, "https://example.invalid", headers={"Accept": "application/json"})

        timeout = client.get.await_args.kwargs["timeout"]
        self.assertEqual((timeout.connect, timeout.read), (1, 2))

    async def test_bounded_get_returns_small_body_and_rejects_oversized_stream(self):
        async def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=httpx.ByteStream(b"12345"),
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        policy = HttpPolicy()
        try:
            bounded = await policy.get_bounded(
                client,
                "https://example.invalid/fixture",
                headers={"Accept": "application/json"},
                max_response_bytes=5,
            )
            self.assertEqual(bounded.content, b"12345")
            with self.assertRaises(HttpResponseTooLarge) as raised:
                await policy.get_bounded(
                    client,
                    "https://example.invalid/fixture",
                    headers={"Accept": "application/json"},
                    max_response_bytes=4,
                )
            self.assertEqual(raised.exception.status_code, 200)
            self.assertEqual(raised.exception.phase, "BODY_STREAM")
            self.assertEqual(raised.exception.bytes_received, 5)
            self.assertEqual(raised.exception.max_response_bytes, 4)
        finally:
            await client.aclose()

    async def test_bounded_get_reports_body_stream_failure_without_message(self):
        class FailingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"partial-private-body"
                raise httpx.ReadError("private exception message")

            async def aclose(self):
                return None

        async def handler(request):
            return httpx.Response(200, stream=FailingStream(), request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(HttpRequestFailure) as raised:
                await HttpPolicy(max_retries=0).get_bounded(
                    client, "https://example.invalid/fixture", headers={}, max_response_bytes=128
                )
            self.assertEqual(raised.exception.diagnostic.phase, "BODY_STREAM")
            self.assertEqual(raised.exception.diagnostic.http_status, 200)
            self.assertEqual(raised.exception.diagnostic.exception_class, "ReadError")
            self.assertEqual(raised.exception.diagnostic.bytes_received, len(b"partial-private-body"))
            self.assertNotIn("private", repr(raised.exception.diagnostic))
        finally:
            await client.aclose()

    async def test_bounded_get_propagates_cancellation_during_body_stream(self):
        started = asyncio.Event()

        class HangingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                return None

        async def handler(request):
            return httpx.Response(200, stream=HangingBody(), request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        task = asyncio.create_task(HttpPolicy(max_retries=0).get_bounded(
            client, "https://example.invalid/fixture", headers={}, max_response_bytes=128
        ))
        try:
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            await client.aclose()

    async def test_bounded_get_records_total_deadline_after_headers(self):
        started = asyncio.Event()

        class HangingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                return None

        async def handler(request):
            return httpx.Response(200, stream=HangingBody(), request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(HttpDeadlineExceeded) as raised:
                await HttpPolicy(total_timeout_seconds=0.02, max_retries=0).get_bounded(
                    client, "https://example.invalid/fixture", headers={}, max_response_bytes=128
                )
            self.assertTrue(started.is_set())
            self.assertEqual(raised.exception.diagnostic.phase, "BODY_STREAM")
            self.assertEqual(raised.exception.diagnostic.http_status, 200)
            self.assertEqual(raised.exception.diagnostic.exception_class, "HttpDeadlineExceeded")
        finally:
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
