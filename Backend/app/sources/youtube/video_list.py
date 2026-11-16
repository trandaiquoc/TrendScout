"""Bounded videos.list request and metadata parser; statistics are not requested."""

import json
import re
from datetime import datetime

import httpx

from app.sources.youtube.errors import YouTubeErrorCode, YouTubeSourceError
from app.sources.youtube.models import (
    ScopedPageToken,
    VideoListPage,
    YouTubeVideoListItem,
    YouTubeVideoListRequest,
)

_PAGE_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{1,2048}$")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CATEGORY_ID = re.compile(r"^[0-9]{1,8}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_RESPONSE_BYTES = 1_000_000
YOUTUBE_VIDEO_LIST_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"


def build_video_list_params(
    request: YouTubeVideoListRequest,
    *,
    page_token: ScopedPageToken | None = None,
) -> dict[str, str | int]:
    """Build the small server-owned parameter allowlist for videos.list."""
    if not isinstance(request, YouTubeVideoListRequest):
        raise YouTubeSourceError(YouTubeErrorCode.INVALID_CONFIGURATION)
    params: dict[str, str | int] = {"part": "snippet"}
    if request.operation == "most_popular":
        params.update({"chart": "mostPopular", "regionCode": "US",
                       "maxResults": request.max_results})
        if request.video_category_id is not None:
            params["videoCategoryId"] = request.video_category_id
        if page_token is not None:
            if (not isinstance(page_token, ScopedPageToken)
                    or page_token.context_key != request.context_key
                    or not isinstance(page_token.value, str)
                    or not _PAGE_TOKEN.fullmatch(page_token.value)):
                raise YouTubeSourceError(YouTubeErrorCode.INVALID_PAGE_TOKEN)
            params["pageToken"] = page_token.value
    elif request.operation == "metadata":
        if page_token is not None:
            raise YouTubeSourceError(YouTubeErrorCode.INVALID_PAGE_TOKEN)
        params["id"] = ",".join(request.video_ids)
    else:
        raise YouTubeSourceError(YouTubeErrorCode.INVALID_CONFIGURATION)
    return params


def parse_video_list_response(
    response: httpx.Response,
    *,
    request: YouTubeVideoListRequest,
) -> VideoListPage:
    """Validate one bounded videos.list response without preserving raw JSON."""
    if not isinstance(request, YouTubeVideoListRequest):
        raise YouTubeSourceError(YouTubeErrorCode.INVALID_CONFIGURATION)
    status = response.status_code
    if 300 <= status < 400:
        raise YouTubeSourceError(
            YouTubeErrorCode.UNEXPECTED_REDIRECT,
            http_status=status,
            request_completed=True,
        )
    if status < 200 or status >= 300:
        raise _http_error(response)
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json" or len(response.content) > MAX_RESPONSE_BYTES:
        raise YouTubeSourceError(
            YouTubeErrorCode.MALFORMED_RESPONSE,
            http_status=status,
            request_completed=True,
        )
    try:
        payload = json.loads(response.content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise YouTubeSourceError(
            YouTubeErrorCode.MALFORMED_RESPONSE,
            http_status=status,
            request_completed=True,
        ) from None
    if not isinstance(payload, dict) or payload.get("kind") != "youtube#videoListResponse":
        raise YouTubeSourceError(
            YouTubeErrorCode.MALFORMED_RESPONSE,
            http_status=status,
            request_completed=True,
        )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > request.max_results:
        raise YouTubeSourceError(
            YouTubeErrorCode.MALFORMED_RESPONSE,
            http_status=status,
            request_completed=True,
        )

    items = tuple(_parse_item(raw, request, status) for raw in raw_items)
    ids = [item.video_id for item in items]
    if len(set(ids)) != len(ids):
        raise YouTubeSourceError(
            YouTubeErrorCode.MALFORMED_RESPONSE,
            http_status=status,
            request_completed=True,
        )
    if request.operation == "metadata" and not set(ids).issubset(request.video_ids):
        raise YouTubeSourceError(
            YouTubeErrorCode.MALFORMED_RESPONSE,
            http_status=status,
            request_completed=True,
        )

    token = payload.get("nextPageToken")
    if token is not None and (
        request.operation != "most_popular"
        or not isinstance(token, str)
        or not _PAGE_TOKEN.fullmatch(token)
    ):
        raise YouTubeSourceError(
            YouTubeErrorCode.INVALID_PAGE_TOKEN,
            http_status=status,
            request_completed=True,
        )
    return VideoListPage(items, token, status)


def _parse_item(raw: object, request: YouTubeVideoListRequest, status: int):
    if not isinstance(raw, dict) or raw.get("kind") != "youtube#video":
        _malformed(status)
    video_id = raw.get("id")
    snippet = raw.get("snippet")
    if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id) or not isinstance(snippet, dict):
        _malformed(status)
    title = snippet.get("title")
    description = snippet.get("description")
    channel_id = snippet.get("channelId")
    channel_title = snippet.get("channelTitle")
    category_id = snippet.get("categoryId")
    broadcast = snippet.get("liveBroadcastContent")
    published_at = snippet.get("publishedAt")
    if (not isinstance(title, str) or len(title) > 1000
            or description is not None and (not isinstance(description, str) or len(description) > 10_000)
            or channel_id is not None and (not isinstance(channel_id, str)
                                           or not _IDENTIFIER.fullmatch(channel_id))
            or channel_title is not None and (not isinstance(channel_title, str)
                                              or len(channel_title) > 1000)
            or category_id is not None and (not isinstance(category_id, str)
                                            or not _CATEGORY_ID.fullmatch(category_id))
            or broadcast is not None and broadcast not in {"none", "live", "upcoming"}):
        _malformed(status)
    parsed_published = _parse_published_at(published_at, status)
    if request.operation == "metadata" and video_id not in request.video_ids:
        _malformed(status)
    return YouTubeVideoListItem(
        video_id=video_id,
        canonical_video_url=f"https://www.youtube.com/watch?v={video_id}",
        title=title,
        description=description,
        channel_id=channel_id,
        channel_title=channel_title,
        published_at=parsed_published,
        category_id=category_id,
        live_broadcast_content=broadcast,
    )


def _parse_published_at(value: object, status: int) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        _malformed(status)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _malformed(status)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _malformed(status)
    return parsed


def _http_error(response: httpx.Response) -> YouTubeSourceError:
    reason = ""
    try:
        payload = json.loads(response.content.decode("utf-8", errors="strict"))
        errors = payload.get("error", {}).get("errors", [])
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = errors[0].get("reason", "")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, RecursionError):
        pass
    if reason in {"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}:
        code = YouTubeErrorCode.QUOTA_EXHAUSTED
    elif reason in {"keyInvalid", "authError", "invalidCredentials"} or response.status_code == 401:
        code = YouTubeErrorCode.UNAUTHORIZED
    elif response.status_code == 429:
        code = YouTubeErrorCode.RATE_LIMITED
    elif response.status_code in {408, 504}:
        code = YouTubeErrorCode.UPSTREAM_TIMEOUT
    elif 500 <= response.status_code <= 599:
        code = YouTubeErrorCode.TRANSIENT_UPSTREAM_ERROR
    elif response.status_code == 400:
        code = YouTubeErrorCode.INVALID_QUERY
    else:
        code = YouTubeErrorCode.PERMISSION_DENIED
    return YouTubeSourceError(code, http_status=response.status_code, request_completed=True)


def _malformed(status: int):
    raise YouTubeSourceError(
        YouTubeErrorCode.MALFORMED_RESPONSE,
        http_status=status,
        request_completed=True,
    )
