"""Offline contract tests for the bounded YouTube videos.list adapter."""

import unittest
from datetime import datetime, timezone

import httpx

from app.sources.youtube.errors import YouTubeErrorCode, YouTubeSourceError
from app.sources.youtube.models import ScopedPageToken, YouTubeVideoListRequest
from app.sources.youtube.video_list import (
    build_video_list_params,
    parse_video_list_response,
)

PLAN_FINGERPRINT = "a" * 64
VIDEO_ID = "abcdefghijk"


def video_item(video_id=VIDEO_ID):
    return {
        "kind": "youtube#video",
        "id": video_id,
        "snippet": {
            "title": "Fixture video",
            "description": "Offline fixture only",
            "channelId": "UCfixture_123",
            "channelTitle": "Fixture channel",
            "publishedAt": "2026-11-15T10:30:00Z",
            "categoryId": "26",
            "liveBroadcastContent": "none",
        },
    }


def json_response(payload, status=200):
    return httpx.Response(
        status,
        headers={"content-type": "application/json; charset=utf-8"},
        json=payload,
    )


class YouTubeVideoListTests(unittest.TestCase):
    def test_most_popular_parameters_are_bounded_and_cursor_is_request_scoped(self):
        request = YouTubeVideoListRequest(
            "most_popular", PLAN_FINGERPRINT, video_category_id="26", max_results=25
        )
        self.assertEqual(build_video_list_params(request), {
            "part": "snippet",
            "maxResults": 25,
            "chart": "mostPopular",
            "regionCode": "US",
            "videoCategoryId": "26",
        })
        token = ScopedPageToken("next-page-token", request.context_key)
        self.assertEqual(
            build_video_list_params(request, page_token=token)["pageToken"],
            "next-page-token",
        )
        with self.assertRaises(YouTubeSourceError) as caught:
            build_video_list_params(
                request, page_token=ScopedPageToken("next-page-token", "wrong-context")
            )
        self.assertEqual(caught.exception.code, YouTubeErrorCode.INVALID_PAGE_TOKEN)

    def test_metadata_ids_are_batched_without_statistics_or_chart_filters(self):
        request = YouTubeVideoListRequest(
            "metadata", PLAN_FINGERPRINT, video_ids=(VIDEO_ID, "lmnopqrstuv")
        )
        params = build_video_list_params(request)
        self.assertEqual(params["id"], f"{VIDEO_ID},lmnopqrstuv")
        self.assertEqual(params["part"], "snippet")
        self.assertNotIn("maxResults", params)
        self.assertNotIn("statistics", params["part"])
        self.assertNotIn("chart", params)
        self.assertNotIn("regionCode", params)
        self.assertNotIn("pageToken", params)
        self.assertNotIn("key", params)

    def test_chart_response_preserves_metadata_and_scoped_timestamps(self):
        request = YouTubeVideoListRequest("most_popular", PLAN_FINGERPRINT)
        response = json_response({
            "kind": "youtube#videoListResponse",
            "nextPageToken": "next-page-token",
            "items": [video_item()],
        })
        page = parse_video_list_response(response, request=request)
        self.assertEqual(page.response_status, 200)
        self.assertEqual(page.next_page_token, "next-page-token")
        item = page.items[0]
        self.assertEqual(item.video_id, VIDEO_ID)
        self.assertEqual(item.canonical_video_url,
                         f"https://www.youtube.com/watch?v={VIDEO_ID}")
        self.assertEqual(item.title, "Fixture video")
        self.assertEqual(item.channel_title, "Fixture channel")
        self.assertEqual(item.published_at,
                         datetime(2026, 11, 15, 10, 30, tzinfo=timezone.utc))

    def test_metadata_response_rejects_ids_outside_requested_batch(self):
        request = YouTubeVideoListRequest("metadata", PLAN_FINGERPRINT, video_ids=(VIDEO_ID,))
        with self.assertRaises(YouTubeSourceError) as caught:
            parse_video_list_response(
                json_response({
                    "kind": "youtube#videoListResponse",
                    "items": [video_item("lmnopqrstuv")],
                }),
                request=request,
            )
        self.assertEqual(caught.exception.code, YouTubeErrorCode.MALFORMED_RESPONSE)

    def test_api_statistics_are_not_returned_by_normalizer(self):
        request = YouTubeVideoListRequest("metadata", PLAN_FINGERPRINT, video_ids=(VIDEO_ID,))
        raw_item = video_item()
        raw_item["statistics"] = {"viewCount": "1000", "likeCount": "30"}
        page = parse_video_list_response(
            json_response({"kind": "youtube#videoListResponse", "items": [raw_item]}),
            request=request,
        )
        self.assertNotIn("view_count", page.items[0].__dataclass_fields__)
        self.assertNotIn("like_count", page.items[0].__dataclass_fields__)
