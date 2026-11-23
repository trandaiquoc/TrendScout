"""Focused contract tests for deterministic YouTube query rotation."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.sources.youtube.query_planner import (
    AUTHENTICATED_MANUAL,
    NORMAL_DEMO,
    OWNER_MANUAL_LIVE,
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
        self.assertEqual(YOUTUBE_QUERY_PLANNER_VERSION,
                         "youtube-curated-slot-and-manual-rotation-v3")
        self.assertEqual(
            select_youtube_query_scopes(now=first_slot, mode=NORMAL_DEMO), first
        )

        next_slot = datetime(2026, 11, 16, 15, 5, tzinfo=LOCAL)
        rotated = select_youtube_query_scopes(now=next_slot)
        self.assertEqual(rotated, ("beauty_personal_care", "travel_lifestyle"))
        self.assertNotEqual(rotated, first)

    def test_selection_fails_closed_outside_a_scheduled_slot(self):
        with self.assertRaisesRegex(ValueError, "scheduled slot"):
            select_youtube_query_scopes(
                now=datetime(2026, 11, 16, 7, 10, tzinfo=LOCAL)
            )

    def test_selection_fails_closed_when_owner_acceptance_is_outside_schedule(self):
        for outside in (
            datetime(2026, 11, 17, 0, 41, tzinfo=LOCAL),
            datetime(2026, 11, 17, 7, 10, tzinfo=LOCAL),
            datetime(2026, 11, 17, 23, 11, tzinfo=LOCAL),
        ):
            with self.subTest(outside=outside):
                with self.assertRaisesRegex(ValueError, "scheduled slot"):
                    select_youtube_query_scopes(now=outside)

    def test_query_count_cannot_exceed_reviewed_cap(self):
        with self.assertRaisesRegex(ValueError, "1..2 query cap"):
            select_youtube_query_scopes(
                now=datetime(2026, 11, 16, 7, 5, tzinfo=LOCAL), max_queries=3
            )

    def test_owner_manual_selection_is_deterministic_at_real_unscheduled_times(self):
        first = datetime(2026, 11, 17, 0, 41, tzinfo=LOCAL)
        second = datetime(2026, 11, 17, 0, 42, tzinfo=LOCAL)
        selected = select_youtube_query_scopes(
            now=first, max_queries=1, mode=OWNER_MANUAL_LIVE
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected, select_youtube_query_scopes(
            now=first, max_queries=1, mode=OWNER_MANUAL_LIVE
        ))
        self.assertIn(selected[0], {
            "fashion", "home_living", "beauty_personal_care",
            "travel_lifestyle", "technology",
        })
        self.assertNotEqual(selected, select_youtube_query_scopes(
            now=second, max_queries=1, mode=OWNER_MANUAL_LIVE
        ))
        with self.assertRaisesRegex(ValueError, "scheduled slot"):
            select_youtube_query_scopes(now=first, mode=NORMAL_DEMO)

    def test_authenticated_manual_selection_rotates_one_scope_outside_schedule(self):
        first = datetime(2026, 11, 17, 0, 41, tzinfo=LOCAL)
        second = datetime(2026, 11, 17, 0, 42, tzinfo=LOCAL)
        selected = select_youtube_query_scopes(
            now=first, max_queries=1, mode=AUTHENTICATED_MANUAL,
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected, select_youtube_query_scopes(
            now=first, max_queries=1, mode=AUTHENTICATED_MANUAL,
        ))
        self.assertNotEqual(selected, select_youtube_query_scopes(
            now=second, max_queries=1, mode=AUTHENTICATED_MANUAL,
        ))


if __name__ == "__main__":
    unittest.main()
