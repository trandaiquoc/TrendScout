"""Bounded parser for the official Google Trends Trending Now RSS export."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from app.sources.catalog import GOOGLE_SOURCE_ID
from app.sources.google_trends.trending_now import normalize_keyword
from app.sources.http_policy import HttpPolicy
from app.sources.serpapi.errors import SerpApiError

RSS_ENDPOINT = "https://trends.google.com/trending/rss?geo=US"
RSS_SOURCE_URL = RSS_ENDPOINT
RSS_PROVIDER_ID = "google_trends_rss"
RSS_TRANSPORT_ID = "google_trends_rss"
RSS_PARSER_VERSION = "google-trends-rss-v1"
_GOOGLE_HOSTS = frozenset({"trends.google.com", "www.google.com", "google.com"})
_GOOGLE_TRENDS_NAMESPACE = "http://www.google.com/trends/hottrends"


@dataclass(frozen=True)
class RssTrendsPage:
    records: tuple[dict, ...]
    provider_record_count: int
    rejected_record_count: int
    provider_data_at: datetime | None
    fetched_at: datetime
    http_status: int


def _child_text(element: ET.Element, name: str, *, namespace: str | None = None) -> str | None:
    if namespace:
        child = element.find(f"{{{namespace}}}{name}")
    else:
        child = element.find(name)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    if len(value) > 128:
        raise ValueError("RSS timestamp is too long")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("RSS timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("RSS timestamp must include a time zone")
    return parsed.astimezone(timezone.utc)


def _safe_google_link(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 2048:
        raise ValueError("RSS item link is too long")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in _GOOGLE_HOSTS
            or parsed.username or parsed.password or parsed.port or parsed.fragment):
        raise ValueError("RSS item link is outside the Google Trends host allowlist")
    return value


def parse_google_trends_rss(response: httpx.Response, *, fetched_at: datetime,
                            data_mode: str = "LIVE") -> RssTrendsPage:
    if (not isinstance(response, httpx.Response) or not isinstance(fetched_at, datetime)
            or fetched_at.tzinfo is None or fetched_at.utcoffset() is None
            or data_mode not in {"LIVE", "SYNTHETIC"}):
        raise SerpApiError("MALFORMED_RESPONSE")
    if response.status_code == 429:
        raw = response.headers.get("Retry-After")
        delay = (HttpPolicy().parse_retry_after(raw, 0, fetched_at)
                 if raw is not None else 1.0)
        if delay > 31_536_000:
            delay = None
        raise SerpApiError("RATE_LIMITED", status=429, retryable=delay is not None,
                           retry_after=delay)
    if response.status_code in {401, 403}:
        raise SerpApiError("PERMISSION_DENIED", status=response.status_code)
    if response.status_code >= 500:
        raise SerpApiError("TRANSIENT_UPSTREAM_ERROR", status=response.status_code, retryable=True)
    if response.status_code != 200:
        raise SerpApiError("PROVIDER_ERROR", status=response.status_code)
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/rss+xml", "application/xml", "text/xml"}:
        raise SerpApiError("MALFORMED_RESPONSE", status=response.status_code)
    body = response.content
    if (not body or b"<!doctype" in body.lower() or b"<!entity" in body.lower()):
        raise SerpApiError("MALFORMED_RESPONSE", status=response.status_code)
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError, RecursionError):
        raise SerpApiError("MALFORMED_RESPONSE", status=response.status_code) from None
    if root.tag != "rss":
        raise SerpApiError("MALFORMED_RESPONSE", status=response.status_code)
    channels = root.findall("channel")
    if len(channels) != 1:
        raise SerpApiError("MALFORMED_RESPONSE", status=response.status_code)
    channel = channels[0]
    channel_link = _child_text(channel, "link")
    if channel_link:
        try:
            _safe_google_link(channel_link)
        except ValueError:
            raise SerpApiError("MALFORMED_RESPONSE", status=response.status_code) from None
    try:
        provider_data_at = _time(_child_text(channel, "lastBuildDate"))
    except ValueError:
        provider_data_at = None

    items = channel.findall("item")
    output, rejected = [], 0
    for item in items:
        try:
            query, identity = normalize_keyword(_child_text(item, "title"))
            item_url = _safe_google_link(_child_text(item, "link"))
            if item_url is None:
                raise ValueError("RSS item has no Google Trends link")
            published = _time(_child_text(item, "pubDate"))
            approx_traffic = _child_text(item, "approx_traffic", namespace=_GOOGLE_TRENDS_NAMESPACE)
            if approx_traffic is not None and (
                    not approx_traffic or len(approx_traffic) > 64 or "\x00" in approx_traffic):
                raise ValueError("invalid approximate traffic bucket")
            description = _child_text(item, "description")
            if description is not None and (len(description) > 4096 or "\x00" in description):
                raise ValueError("RSS description exceeds bounds")
        except (AttributeError, TypeError, ValueError):
            rejected += 1
            continue
        cluster_id = hashlib.sha256(
            f"{identity}\0{published.isoformat() if published else ''}\0{item_url}".encode("utf-8")
        ).hexdigest()
        output.append({
            "source_id": GOOGLE_SOURCE_ID,
            "provider_id": RSS_PROVIDER_ID,
            "transport_id": RSS_TRANSPORT_ID,
            "data_source": "GOOGLE_TRENDS",
            "discovery_mode": "RSS_LIMITED_DISCOVERY",
            "plan_id": "google-trends-rss-us-v1",
            "plan_version": 1,
            "canonical_keyword": query,
            "canonical_keyword_identity": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            "region": "US",
            "language": "en",
            "time_window_hours": None,
            "provider_data_at": provider_data_at.isoformat() if provider_data_at else None,
            "provider_created_at": None,
            "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
            "source_url": RSS_SOURCE_URL,
            "delivery_state": "FRESH",
            "data_mode": data_mode,
            "trend_clusters": [{
                "trend_cluster_id": cluster_id,
                "parent_keyword_identity": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "started_at": None,
                "ended_at": None,
                "active": None,
                "search_volume": None,
                "search_volume_display": approx_traffic,
                "search_volume_semantics": "RSS_APPROX_TRAFFIC_BUCKET" if approx_traffic else None,
                "increase_percentage": None,
                "categories": None,
                "related_queries": None,
                "rss_published_at": published.isoformat() if published else None,
                "rss_description": description,
                "source_item_url": item_url,
                "provider_data_at": provider_data_at.isoformat() if provider_data_at else None,
                "source_url": RSS_SOURCE_URL,
            }],
            "quality_flags": ["RSS_LIMITED_COVERAGE", "NO_COMPATIBLE_BASELINE",
                              "RELATED_QUERY_BREAKDOWN_UNAVAILABLE"],
        })
    return RssTrendsPage(
        tuple(output), len(items), rejected,
        provider_data_at, fetched_at.astimezone(timezone.utc), response.status_code,
    )
