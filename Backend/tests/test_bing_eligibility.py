import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.sources.bing_search.eligibility import eligible_slot

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


class BingEligibilityTests(unittest.TestCase):
    def test_only_current_short_slot_is_eligible_and_missed_slots_do_not_catch_up(self):
        self.assertEqual(eligible_slot(datetime(2026, 11, 16, 7, 5, tzinfo=TZ)),
                         "2026-11-16T07:00:00+07:00")
        self.assertIsNone(eligible_slot(datetime(2026, 11, 16, 7, 11, tzinfo=TZ)))
        self.assertIsNone(eligible_slot(datetime(2026, 11, 16, 10, 0, tzinfo=TZ)))

    def test_eight_hour_spacing_and_timezone_aware_timestamps_are_required(self):
        last = datetime(2026, 11, 16, 7, 5, tzinfo=TZ)
        self.assertIsNone(eligible_slot(datetime(2026, 11, 16, 15, 4, tzinfo=TZ),
                                        last_attempt_at=last))
        self.assertEqual(eligible_slot(datetime(2026, 11, 16, 15, 5, tzinfo=TZ),
                                       last_attempt_at=last),
                         "2026-11-16T15:00:00+07:00")
        with self.assertRaises(ValueError):
            eligible_slot(datetime(2026, 11, 16, 8))

    def test_utc_timestamp_is_converted_to_the_configured_local_schedule(self):
        self.assertEqual(eligible_slot(datetime(2026, 11, 16, 0, 5, tzinfo=timezone.utc)),
                         "2026-11-16T07:00:00+07:00")


if __name__ == "__main__":
    unittest.main()
