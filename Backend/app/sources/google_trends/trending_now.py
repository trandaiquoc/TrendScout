"""Strict parser and normalization for the SerpApi Trending Now engine.

Search volume and increase values remain provider-reported signals. This module
does not estimate a baseline, score growth, or treat related queries as rows.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.analysis.keyword_identity import normalize_keyword_text
from app.sources.catalog import GOOGLE_SOURCE_ID
from app.sources.http_policy import HttpPolicy
from app.sources.serpapi.errors import SerpApiError

TRENDING_NOW_ENGINE = "google_trends_trending_now"
TRENDING_NOW_PLAN_ID = "google-trending-now-us-en"
TRENDING_NOW_PLAN_VERSION = 1
TRENDING_NOW_WINDOW_HOURS = 24
TRENDING_NOW_SOURCE_URL = "https://trends.google.com/trending?geo=US&hl=en-US"
TRENDING_NOW_PROVIDER_ID = "serpapi_google_trends_trending_now"
TRENDING_NOW_TRANSPORT_ID = "serpapi"
TRENDING_NOW_PARSER_VERSION = "google-trending-now-v2"
_MAX_PROVIDER_TIMESTAMP = 4_102_444_800  # 2100-01-01 UTC


@dataclass(frozen=True)
class GoogleTrendsRequest:
    """Fixed US/English/24-hour server-owned request; no browser parameters."""

    source_id: str
    policy_fingerprint: str
    plan_id: str = TRENDING_NOW_PLAN_ID
    plan_version: int = TRENDING_NOW_PLAN_VERSION
    engine: str = TRENDING_NOW_ENGINE
    geo: str = "US"
    language: str = "en"
    hours: int = TRENDING_NOW_WINDOW_HOURS
    force_refresh: bool = False


@dataclass(frozen=True)
class TrendingNowPage:
    records: tuple[dict, ...]
    provider_record_count: int
    rejected_record_count: int
    provider_status: str
    provider_created_at: datetime | None
    provider_data_at: datetime | None
    http_status: int
    delivery_state: str


def normalize_keyword(value: str) -> tuple[str, str]:
    try:
        display, identity = normalize_keyword_text(value)
    except ValueError:
        raise ValueError("trend query must be valid bounded text") from None
    if len(display) > 256:
        raise ValueError("trend query is empty or outside the bounded text contract")
    return display, identity


def canonical_keyword_identity(value: str) -> str:
    _, identity = normalize_keyword(value)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build_trending_now_params(request: GoogleTrendsRequest) -> dict[str, str | int]:
    if (not isinstance(request, GoogleTrendsRequest)
            or request.source_id != GOOGLE_SOURCE_ID
            or request.plan_id != TRENDING_NOW_PLAN_ID
            or request.plan_version != TRENDING_NOW_PLAN_VERSION
            or request.engine != TRENDING_NOW_ENGINE
            or request.geo != "US" or request.language != "en"
            or request.hours != TRENDING_NOW_WINDOW_HOURS
            or type(request.force_refresh) is not bool
            or not isinstance(request.policy_fingerprint, str)
            or len(request.policy_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in request.policy_fingerprint)):
        raise SerpApiError("INVALID_CONFIGURATION")
    return {"engine": TRENDING_NOW_ENGINE, "geo": "US", "hl": "en",
            "hours": TRENDING_NOW_WINDOW_HOURS, "output": "json"}


def _provider_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(
                tzinfo=timezone.utc
            )
            return parsed
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    return None


def _epoch(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise ValueError(f"invalid {field}")
    # Check the finite application range before converting huge Python integers
    # to float; math.isfinite(10**10000) itself raises OverflowError.
    if value < 0 or value > _MAX_PROVIDER_TIMESTAMP:
        raise ValueError(f"invalid {field}")
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"invalid {field}")
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError(f"invalid {field}") from None


def _optional_percentage(value: object) -> int | float | None:
    if value is None:
        return None
    if (type(value) not in {int, float} or value < 0 or value > 1_000_000_000
            or type(value) is float and not math.isfinite(value)):
        raise ValueError("invalid increase percentage")
    return value


def _categories(value: object) -> list[dict] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("invalid categories")
    if len(value) > 5000:
        raise ValueError("category count exceeds bounded response size")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid category record")
        category_id = item.get("id")
        if category_id is not None and (type(category_id) is not int or category_id < 0):
            raise ValueError("invalid category id")
        name = item.get("name")
        if name is not None:
            if not isinstance(name, str) or not name.strip() or len(name) > 128 or "\x00" in name:
                raise ValueError("invalid category name")
            name = " ".join(name.split())
        result.append({"id": category_id, "name": name})
    return result


def _breakdown(value: object) -> tuple[list[str] | None, int]:
    if value is None:
        return None, 0
    if not isinstance(value, list):
        raise ValueError("invalid trend breakdown")
    if len(value) > 5000:
        raise ValueError("trend breakdown count exceeds bounded response size")
    accepted, rejected = [], 0
    for item in value:
        if not isinstance(item, str):
            rejected += 1
            continue
        try:
            display, _ = normalize_keyword(item)
        except ValueError:
            rejected += 1
            continue
        if display not in accepted:
            accepted.append(display)
    return accepted, rejected


def _parse_record(item: object) -> tuple[dict | None, int]:
    if not isinstance(item, dict):
        return None, 1
    try:
        query, identity = normalize_keyword(item.get("query"))
        started = _epoch(item.get("start_timestamp"), field="start timestamp")
        ended = _epoch(item.get("end_timestamp"), field="end timestamp")
        active = item.get("active")
        if active is not None and type(active) is not bool:
            raise ValueError("invalid active status")
        volume = item.get("search_volume")
        if volume is not None and (type(volume) is not int or volume < 0):
            raise ValueError("invalid search volume")
        display = item.get("search_volume_display")
        if display is not None and (
                not isinstance(display, str) or not display.strip() or len(display) > 64
                or "\x00" in display):
            raise ValueError("invalid search-volume display")
        percentage = _optional_percentage(item.get("increase_percentage"))
        categories = _categories(item.get("categories"))
        related, rejected_related = _breakdown(item.get("trend_breakdown"))
    except (AttributeError, TypeError, ValueError):
        return None, 1

    if started and ended and ended < started:
        return None, 1
    identity_digest = hashlib.sha256(json.dumps(
        [TRENDING_NOW_PLAN_ID, identity, started.isoformat() if started else None,
         categories, related, volume, display, percentage, active],
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()
    row = {
        "query": query,
        "source_keyword_raw": item.get("query"),
        "canonical_keyword": query,
        "canonical_keyword_identity": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "trend_cluster_id": identity_digest,
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "active": active,
        "search_volume": volume,
        "search_volume_display": display,
        "search_volume_semantics": "PROVIDER_REPORTED_APPROXIMATE_BUCKET",
        "increase_percentage": percentage,
        "categories": categories,
        "related_queries": related,
        "provider_id": TRENDING_NOW_PROVIDER_ID,
        "transport_id": TRENDING_NOW_TRANSPORT_ID,
        "discovery_mode": "SERPAPI_TRENDING_NOW",
        "region": "US",
        "language": "en",
        "time_window_hours": TRENDING_NOW_WINDOW_HOURS,
        "source_url": TRENDING_NOW_SOURCE_URL,
    }
    return row, rejected_related


def parse_trending_now_response(response: httpx.Response, *, request: GoogleTrendsRequest,
                                sensitive_key: str) -> TrendingNowPage:
    """Validate the dedicated Trends JSON shape; never parse it as organic results."""
    if not isinstance(response, httpx.Response) or not isinstance(request, GoogleTrendsRequest):
        raise SerpApiError("MALFORMED_RESPONSE")
    status = response.status_code
    if not 200 <= status < 300:
        try:
            payload = json.loads(response.content.decode("utf-8", errors="strict"))
        except (UnicodeError, ValueError, RecursionError):
            payload = None
        message = payload.get("error") if isinstance(payload, dict) else None
        if status == 429 and isinstance(message, str) and "run out of searches" in message.lower():
            raise SerpApiError("QUOTA_EXHAUSTED", status=status)
        if status == 429:
            raw = response.headers.get("Retry-After")
            delay = HttpRetryAfter.retry_delay(raw)
            raise SerpApiError("RATE_LIMITED", status=status, retryable=delay is not None,
                               retry_after=delay)
        code = {300: "UNEXPECTED_REDIRECT", 301: "UNEXPECTED_REDIRECT",
                302: "UNEXPECTED_REDIRECT", 303: "UNEXPECTED_REDIRECT",
                307: "UNEXPECTED_REDIRECT", 308: "UNEXPECTED_REDIRECT",
                401: "UNAUTHORIZED", 403: "PERMISSION_DENIED", 408: "UPSTREAM_TIMEOUT"}.get(
            status, "TRANSIENT_UPSTREAM_ERROR" if status >= 500 else "PROVIDER_ERROR")
        raise SerpApiError(code, status=status, retryable=status == 408 or status >= 500)
    try:
        build_trending_now_params(request)
        payload = json.loads(response.content.decode("utf-8", errors="strict"))
    except (SerpApiError, UnicodeError, ValueError, RecursionError):
        raise SerpApiError("MALFORMED_RESPONSE", status=status) from None
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json" or not isinstance(payload, dict):
        raise SerpApiError("MALFORMED_RESPONSE", status=status)
    metadata = payload.get("search_metadata")
    parameters = payload.get("search_parameters")
    if (not isinstance(metadata, dict) or metadata.get("status") not in {"Success", "Cached"}
            or not isinstance(parameters, dict)
            or parameters.get("engine") != TRENDING_NOW_ENGINE
            or parameters.get("geo") != "US"
            or parameters.get("hl", "en") != "en"
            or parameters.get("hours", TRENDING_NOW_WINDOW_HOURS) != TRENDING_NOW_WINDOW_HOURS):
        raise SerpApiError("MALFORMED_RESPONSE", status=status)
    raw_rows = payload.get("trending_searches")
    if not isinstance(raw_rows, list):
        raise SerpApiError("MALFORMED_RESPONSE", status=status)
    # The bounded HTTP body is the population bound. We do not silently slice rows.
    rows, rejected = [], 0
    for raw_row in raw_rows:
        row, invalid_related = _parse_record(raw_row)
        if row is None:
            rejected += 1
            continue
        rows.append(row)
        rejected += invalid_related
    encoded_records = json.dumps(rows, ensure_ascii=False, allow_nan=False)
    if sensitive_key and sensitive_key in encoded_records:
        raise SerpApiError("MALFORMED_RESPONSE", status=status)
    created = _provider_time(metadata.get("created_at"))
    data_at = _provider_time(metadata.get("processed_at")) or created
    return TrendingNowPage(
        tuple(rows), len(raw_rows), rejected,
        "CACHED" if metadata.get("status") == "Cached" else "SUCCESS",
        created, data_at, status,
        "CACHED" if metadata.get("status") == "Cached" else "FRESH",
    )


class HttpRetryAfter:
    """Tiny parser local to this contract, avoiding exception text retention."""

    @staticmethod
    def retry_delay(value: str | None) -> float | None:
        if value is None:
            return 1.0
        # Match the existing shared HTTP policy, including HTTP-date values.
        delay = HttpPolicy().parse_retry_after(value, 0, datetime.now(timezone.utc))
        return delay if delay <= 31_536_000 else None


def normalize_trending_candidates(page: TrendingNowPage, *, fetched_at: datetime,
                                 data_mode: str = "LIVE") -> list[dict]:
    if (not isinstance(page, TrendingNowPage) or not isinstance(fetched_at, datetime)
            or fetched_at.tzinfo is None or fetched_at.utcoffset() is None
            or data_mode not in {"LIVE", "SYNTHETIC"}):
        raise ValueError("validated provider page and fetch time are required")
    fetched = fetched_at.astimezone(timezone.utc).isoformat()
    groups: dict[str, dict] = {}
    cluster_sets: dict[str, set[str]] = {}
    for row in page.records:
        query, identity = normalize_keyword(row["canonical_keyword"])
        candidate_key = row["canonical_keyword_identity"]
        cluster_id = row["trend_cluster_id"]
        if candidate_key not in groups:
            groups[candidate_key] = {
                "source_id": GOOGLE_SOURCE_ID,
                "provider_id": TRENDING_NOW_PROVIDER_ID,
                "transport_id": TRENDING_NOW_TRANSPORT_ID,
                "data_source": "GOOGLE_TRENDS",
                "discovery_mode": "SERPAPI_TRENDING_NOW",
                "plan_id": TRENDING_NOW_PLAN_ID,
                "plan_version": TRENDING_NOW_PLAN_VERSION,
                "canonical_keyword": query,
                "canonical_keyword_identity": candidate_key,
                "region": "US",
                "language": "en",
                "time_window_hours": TRENDING_NOW_WINDOW_HOURS,
                "provider_data_at": page.provider_data_at.isoformat()
                    if page.provider_data_at else None,
                "provider_created_at": page.provider_created_at.isoformat()
                    if page.provider_created_at else None,
                "fetched_at": fetched,
                "source_url": TRENDING_NOW_SOURCE_URL,
                "delivery_state": page.delivery_state,
                "data_mode": data_mode,
                "trend_clusters": [],
                "quality_flags": ["PROVIDER_APPROXIMATE_BUCKET", "NO_COMPATIBLE_BASELINE"],
            }
            cluster_sets[candidate_key] = set()
        if cluster_id in cluster_sets[candidate_key]:
            continue
        cluster_sets[candidate_key].add(cluster_id)
        groups[candidate_key]["trend_clusters"].append({
            "trend_cluster_id": cluster_id,
            "parent_keyword_identity": candidate_key,
            "source_keyword_raw": row["source_keyword_raw"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "active": row["active"],
            "search_volume": row["search_volume"],
            "search_volume_display": row["search_volume_display"],
            "search_volume_semantics": row["search_volume_semantics"],
            "increase_percentage": row["increase_percentage"],
            "categories": row["categories"],
            "related_queries": ([{"query": related,
                                   "parent_keyword_identity": candidate_key,
                                   "parent_cluster_id": cluster_id}
                                  for related in row["related_queries"]]
                                 if row["related_queries"] is not None else None),
            "provider_data_at": page.provider_data_at.isoformat()
                if page.provider_data_at else None,
            "source_url": TRENDING_NOW_SOURCE_URL,
        })
    result = list(groups.values())
    for candidate in result:
        candidate["trend_clusters"].sort(key=lambda cluster: cluster["trend_cluster_id"])
    result.sort(key=lambda item: (item["canonical_keyword_identity"], item["canonical_keyword"].casefold()))
    return result
