"""Validate provider search items and create source-scoped records and evidence."""

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.sources.youtube.models import (
    MetricDescriptor,
    QueryContext,
    QueryEvidence,
    YouTubeSearchRecord,
)

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_THUMBNAIL_HOSTS = frozenset({"i.ytimg.com", "yt3.ggpht.com"})
_MAX_TITLE_LENGTH = 1000
_MAX_DESCRIPTION_LENGTH = 5000
_MAX_CHANNEL_TITLE_LENGTH = 500


def normalize_items(
    context: QueryContext,
    items: tuple[object, ...],
    *,
    fetched_at: datetime,
    page_index: int,
    rank_offset: int,
    provider_response_status: int,
) -> tuple[tuple[YouTubeSearchRecord, ...], tuple[str, ...]]:
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("Fetch timestamp must be timezone-aware")
    fetched_utc = fetched_at.astimezone(timezone.utc)
    records = []
    rejection_reasons = []
    for item_index, item in enumerate(items):
        record, rejection_reason = _normalize_item(
            context,
            item,
            fetched_at=fetched_utc,
            page_index=page_index,
            result_rank=rank_offset + item_index + 1,
            provider_response_status=provider_response_status,
        )
        if record is None:
            rejection_reasons.append(rejection_reason or "INVALID_PROVIDER_ITEM")
        else:
            records.append(record)
    return tuple(records), tuple(rejection_reasons)


def _normalize_item(
    context: QueryContext,
    item: object,
    *,
    fetched_at: datetime,
    page_index: int,
    result_rank: int,
    provider_response_status: int,
) -> tuple[YouTubeSearchRecord | None, str | None]:
    if not isinstance(item, dict) or item.get("kind") != "youtube#searchResult":
        return None, "INVALID_SEARCH_RESULT_SHAPE"
    video = item.get("id")
    if not isinstance(video, dict) or video.get("kind") != "youtube#video":
        return None, "INVALID_VIDEO_RESOURCE"
    video_id = video.get("videoId")
    if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
        return None, "MISSING_OR_INVALID_VIDEO_ID"
    snippet = item.get("snippet")
    if not isinstance(snippet, dict):
        return None, "INVALID_SNIPPET"

    flags = ["SYNTHETIC_OFFLINE_EVIDENCE", "QUERY_SEARCH_RESULT"]
    try:
        title = _optional_text(snippet.get("title"), _MAX_TITLE_LENGTH)
        description = _optional_text(snippet.get("description"), _MAX_DESCRIPTION_LENGTH)
        channel_id = _optional_text(snippet.get("channelId"), 256)
        channel_title = _optional_text(snippet.get("channelTitle"), _MAX_CHANNEL_TITLE_LENGTH)
    except (UnicodeError, ValueError):
        return None, "INVALID_TEXT_FIELD"
    if title is None:
        flags.append("MISSING_TITLE")
    if channel_id is None:
        flags.append("MISSING_CHANNEL_ID")
    if channel_title is None:
        flags.append("MISSING_CHANNEL_TITLE")
    if description is None:
        flags.append("MISSING_DESCRIPTION")

    published_at = None
    raw_published = snippet.get("publishedAt")
    if raw_published is None:
        flags.append("MISSING_PUBLISHED_AT")
    elif isinstance(raw_published, str):
        try:
            published_at = _parse_timestamp(raw_published)
        except ValueError:
            return None, "INVALID_PUBLISHED_AT"
    else:
        return None, "INVALID_PUBLISHED_AT_TYPE"

    thumbnails, invalid_thumbnail = _thumbnail_urls(snippet.get("thumbnails"))
    if invalid_thumbnail:
        flags.append("INVALID_THUMBNAIL_URL_OMITTED")
    live_content = snippet.get("liveBroadcastContent")
    if live_content is not None and (
        not isinstance(live_content, str) or live_content not in {"live", "none", "upcoming"}
    ):
        live_content = None
        flags.append("UNKNOWN_LIVE_BROADCAST_CONTENT")

    evidence = QueryEvidence(
        source_id=context.source_id,
        provider_id=context.provider_id,
        query_plan_id=context.plan_id,
        query_plan_version=context.plan_version,
        query_plan_fingerprint=context.plan_fingerprint,
        query_fingerprint=context.query_fingerprint,
        canonical_query=context.canonical_query,
        taxonomy_scope_label=context.taxonomy_scope_label,
        market_filter=context.region_code,
        language_filter=context.relevance_language,
        published_after=context.published_after,
        fetched_at=fetched_at,
        requested_page=page_index,
        result_rank=result_rank,
        page_index=page_index,
        provider_response_status=provider_response_status,
    )
    return YouTubeSearchRecord(
        source_id=context.source_id,
        provider_id=context.provider_id,
        data_mode="SYNTHETIC",
        record_schema_version=1,
        provider_video_id=video_id,
        canonical_video_url=f"https://www.youtube.com/watch?v={video_id}",
        title=title,
        description=description,
        channel_id=channel_id,
        channel_title=channel_title,
        published_at=published_at,
        fetched_at=fetched_at,
        query_plan_id=context.plan_id,
        query_plan_version=context.plan_version,
        query_fingerprint=context.query_fingerprint,
        query_evidence=(evidence,),
        market_filter=context.region_code,
        language_filter=context.relevance_language,
        result_rank=result_rank,
        page_index=page_index,
        metric=MetricDescriptor(
            name="result_rank",
            unit="ordinal position",
            meaning=(
                "Position in query-scoped YouTube search results; not views, search volume, "
                "trending rank, or product demand."
            ),
            value=result_rank,
        ),
        quality_flags=tuple(flags),
        provider_response_status=provider_response_status,
        thumbnail_urls=thumbnails,
        live_broadcast_content=live_content,
    ), None


def _optional_text(value: object, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Invalid provider text field")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("Provider text is not valid Unicode") from exc
    if any(ord(character) == 0 for character in value):
        raise ValueError("Provider text contains a null character")
    return value


def _parse_timestamp(value: str) -> datetime:
    if len(value) > 64:
        raise ValueError("Timestamp exceeds size limit")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _thumbnail_urls(value: object) -> tuple[tuple[str, ...], bool]:
    if value is None:
        return (), False
    if not isinstance(value, dict):
        return (), True
    urls = []
    invalid = False
    for thumbnail in value.values():
        if not isinstance(thumbnail, dict):
            invalid = True
            continue
        url = thumbnail.get("url")
        if not isinstance(url, str) or len(url) > 2048:
            invalid = True
            continue
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            invalid = True
            continue
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _THUMBNAIL_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or not parsed.path.startswith("/")
        ):
            invalid = True
            continue
        urls.append(url)
    return tuple(urls), invalid
