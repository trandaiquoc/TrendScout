"""Prefer approved Trending Now discovery with an independently approved RSS fallback."""

from __future__ import annotations


class GoogleDiscoveryFallbackHandler:
    def __init__(self, *, trending_now=None, rss=None):
        for handler in (trending_now, rss):
            if handler is not None and not callable(handler):
                raise ValueError("Google discovery handlers must be server-owned callables")
        if trending_now is None and rss is None:
            raise ValueError("at least one approved Google discovery path is required")
        self.trending_now, self.rss = trending_now, rss

    async def __call__(self, context):
        if self.trending_now is None:
            return await self.rss(context)
        primary = await self.trending_now(context)
        if primary.get("status") in {"SUCCESS", "PARTIAL"} or self.rss is None:
            return primary
        fallback = await self.rss(context)
        if fallback.get("status") not in {"SUCCESS", "PARTIAL"}:
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
