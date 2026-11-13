"""Scoped fail-fast guard for tests that must never construct real HTTP transports."""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import httpx

OFFLINE_GUARD_TOTALS = {"real_constructions": 0, "fake_constructions": 0,
                        "requests": 0, "closed": 0}


@contextmanager
def strict_offline_http_guard():
    """Replace AsyncHTTPTransport and trap even references to its original class."""
    original_transport = httpx.AsyncHTTPTransport
    counts = {"real_constructions": 0, "fake_constructions": 0,
              "requests": 0, "closed": 0}

    def reject_real_construction(_self, *args, **kwargs):
        counts["real_constructions"] += 1
        OFFLINE_GUARD_TOTALS["real_constructions"] += 1
        raise AssertionError("strict offline guard blocked real AsyncHTTPTransport construction")

    class OfflineAsyncHTTPTransport(httpx.AsyncBaseTransport):
        def __init__(self, **kwargs):
            counts["fake_constructions"] += 1
            OFFLINE_GUARD_TOTALS["fake_constructions"] += 1
            self.kwargs = kwargs
            self.is_closed = False

        async def handle_async_request(self, request):
            counts["requests"] += 1
            OFFLINE_GUARD_TOTALS["requests"] += 1
            raise AssertionError("strict offline transport cannot send HTTP requests")

        async def aclose(self):
            if not self.is_closed:
                self.is_closed = True
                counts["closed"] += 1
                OFFLINE_GUARD_TOTALS["closed"] += 1

    with ExitStack() as stack:
        stack.enter_context(patch.object(original_transport, "__init__", reject_real_construction))
        stack.enter_context(patch.object(httpx, "AsyncHTTPTransport", OfflineAsyncHTTPTransport))
        yield counts, OfflineAsyncHTTPTransport
