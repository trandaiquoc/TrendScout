"""Focused contract tests for deterministic YouTube query rotation."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.sources.youtube.query_planner import (
    YOUTUBE_QUERY_PLANNER_VERSION,
    select_youtube_query_scopes,
)

LOCAL = ZoneInfo("Asia/Ho_Chi_Minh")


class YouTubeQueryPlannerTests(unittest.TestCase):
    def test_slot_selection_is_deterministic_bounded_and_rotates_topics(self):
        first_slot = datetime(2026, 11, 16, 7, 5, tzinfo=LOCAL)
        first = select_youtube_query_scopes(now=first_slot)
        self.assertEqual(first, ("fashion", "home_living"))
        self.assertEqual(select_youtube_query_scopes(now=first_slot), first)
        self.assertEqual(len(first), 2)
        self.assertEqual(YOUTUBE_QUERY_PLANNER_VERSION, "youtube-curated-slot-rotation-v1")

        next_slot = datetime(2026, 11, 16, 15, 5, tzinfo=LOCAL)
        rotated = select_youtube_query_scopes(now=next_slot)
        self.assertEqual(rotated, ("beauty_personal_care", "travel_lifestyle"))
        self.assertNotEqual(rotated, first)

    def test_selection_fails_closed_outside_a_scheduled_slot(self):
        with self.assertRaisesRegex(ValueError, "scheduled slot"):
            select_youtube_query_scopes(
                now=datetime(2026, 11, 16, 7, 10, tzinfo=LOCAL)
            )

    def test_query_count_cannot_exceed_reviewed_cap(self):
        with self.assertRaisesRegex(ValueError, "1..2 query cap"):
            select_youtube_query_scopes(
                now=datetime(2026, 11, 16, 7, 5, tzinfo=LOCAL), max_queries=3
            )


if __name__ == "__main__":
    unittest.main()
