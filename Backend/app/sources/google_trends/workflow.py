"""Prefer approved Trending Now discovery with an independently approved RSS fallback."""

from __future__ import annotations


class GoogleDiscoveryFallbackHandler:
    def __init__(self, *, trending_now=None, rss=None, cached_fallback=None):
        for handler in (trending_now, rss, cached_fallback):
            if handler is not None and not callable(handler):
                raise ValueError("Google discovery handlers must be server-owned callables")
        if trending_now is None and rss is None and cached_fallback is None:
            raise ValueError("at least one approved Google discovery path is required")
        self.trending_now, self.rss = trending_now, rss
        self.cached_fallback = cached_fallback

    @staticmethod
    def _successful_result(result):
        return (isinstance(result, dict)
                and result.get("status") in {"SUCCESS", "PARTIAL"}
                and isinstance(result.get("records"), list)
                and bool(result["records"]))

    def _cache_after_failure(self, context, *attempts):
        if self.cached_fallback is None:
            return None
        try:
            cached = self.cached_fallback(context)
        except Exception:
            return None
        if (not isinstance(cached, dict)
                or cached.get("data_mode") != "LIVE"
                or cached.get("status") not in {"SUCCESS", "PARTIAL"}
                or cached.get("delivery_state") not in {"CACHED", "STALE_CACHE"}
                or not isinstance(cached.get("records"), list)
                or type(cached.get("record_count")) is not int
                or cached["record_count"] != len(cached["records"])
                or not cached["records"]):
            return None
        cached = dict(cached)
        coverage = cached.get("coverage")
        coverage = dict(coverage) if isinstance(coverage, dict) else {}
        error_codes = []
        for result in attempts:
            code = result.get("error_code") if isinstance(result, dict) else None
            if isinstance(code, str) and code.isascii() and code.replace("_", "").isalnum():
                error_codes.append(code[:64])
        coverage["refresh_attempt_status"] = "FAILED"
        if error_codes:
            coverage["refresh_error_codes"] = error_codes
        request_counts = [self._provider_request_count(result) for result in attempts]
        if all(type(count) is int for count in request_counts):
            request_count = sum(request_counts)
            coverage["provider_request_count"] = request_count
            coverage["refresh_provider_request_count"] = request_count
            cached["provider_request_count"] = request_count
        else:
            coverage.pop("provider_request_count", None)
            cached.pop("provider_request_count", None)
        cached["coverage"] = coverage
        if cached["delivery_state"] == "STALE_CACHE":
            cached["status"] = "PARTIAL"
            if error_codes:
                cached["error_code"] = error_codes[-1]
        return cached

    @staticmethod
    def _provider_request_count(result):
        if not isinstance(result, dict):
            return None
        value = result.get("provider_request_count")
        if type(value) is int and value >= 0:
            return value
        coverage = result.get("coverage")
        if not isinstance(coverage, dict):
            return None
        value = coverage.get("provider_request_count")
        if type(value) is int and value >= 0:
            return value
        if coverage.get("attempted_calls") == 0:
            return 0
        return None

    async def __call__(self, context):
        if self.trending_now is None:
            result = await self.rss(context)
            if self._successful_result(result):
                return result
            return self._cache_after_failure(context, result) or result
        primary = await self.trending_now(context)
        if self._successful_result(primary):
            return primary
        if self.rss is None:
            return self._cache_after_failure(context, primary) or primary
        fallback = await self.rss(context)
        if not self._successful_result(fallback):
            cached = self._cache_after_failure(context, primary, fallback)
            if cached is not None:
                return cached
            coverage = dict(fallback.get("coverage", {}))
            coverage["trending_now_error_code"] = primary.get("error_code", "UNKNOWN")
            return {**fallback, "coverage": coverage,
                    "status": "FAILED" if fallback.get("status") == "FAILED" else "PARTIAL"}
        coverage = dict(fallback.get("coverage", {}))
        coverage.update(
            fallback_from="SERPAPI_TRENDING_NOW",
            trending_now_error_code=primary.get("error_code", "UNKNOWN"),
        )
        flags = sorted(set(fallback.get("quality_flags", []))
                       | {"TRENDING_NOW_UNAVAILABLE_RSS_FALLBACK"})
        return {**fallback, "coverage": coverage, "quality_flags": flags}

    def __call_mode__(self):
        for handler in (self.trending_now, self.rss):
            if handler is not None:
                return handler.__call_mode__()
        return None
