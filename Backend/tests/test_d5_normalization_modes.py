"""Synthetic contract checks for origin separation; no live data is fetched."""

import unittest
from dataclasses import replace

from test_d5_public_admission import NOW

from app.sources.catalog import GOOGLE_SOURCE_ID
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.serpapi.normalization import merge_record, normalize_items
from app.sources.serpapi.pagination import build_contexts
from app.sources.youtube.adapter import YouTubeSearchAdapter
from app.sources.youtube.normalizer import normalize_items as normalize_video
from app.sources.youtube.pagination import build_execution_plan


class NormalizationModesTests(unittest.TestCase):
    def test_serp_synthetic_provenance_cannot_merge_with_other_mode(self):
        context = build_contexts(DEFAULT_QUERY_PLAN, GOOGLE_SOURCE_ID, NOW)[0]
        records, _ = normalize_items(context, ({"position": 1, "link": "https://example.org/a"},),
            fetched_at=NOW, page=1, offset=0, http_status=200, cache_state="UNKNOWN",
            provider_created_at=None)
        self.assertEqual(records[0].data_mode, "SYNTHETIC")
        with self.assertRaises(ValueError):
            merge_record(records[0], replace(records[0], data_mode="LIVE"))
        with self.assertRaises(ValueError):
            normalize_items(context, (), fetched_at=NOW, page=1, offset=0, http_status=200,
                            cache_state="UNKNOWN", provider_created_at=None, data_mode="INVALID")

    def test_youtube_synthetic_provenance_cannot_merge_with_other_mode(self):
        context = build_execution_plan(DEFAULT_QUERY_PLAN, NOW).queries[0]
        items = ({"kind": "youtube#searchResult", "id": {"kind": "youtube#video",
                 "videoId": "AbCdEf12345"}, "snippet": {"title": "fixture"}},)
        records, _ = normalize_video(context, items, fetched_at=NOW, page_index=1,
                                     rank_offset=0, provider_response_status=200)
        self.assertEqual(records[0].data_mode, "SYNTHETIC")
        with self.assertRaises(ValueError):
            YouTubeSearchAdapter._merge_record(records[0], replace(records[0], data_mode="LIVE"))
