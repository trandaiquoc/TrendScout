import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.jobs.public_admission import AdmissionDenied
from app.sources.refresh_eligibility import (
    BING_REFRESH_STATE_KEY,
    YOUTUBE_REFRESH_STATE_KEY,
    eligible_slot,
    manual_candidate_slot,
    manual_schedule_metadata,
    reserve_manual_source_slot,
    reserve_source_slot,
    schedule_metadata,
)
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore

LOCAL = ZoneInfo("Asia/Ho_Chi_Minh")


class RefreshEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = LocalSQLiteAtomicStateStore.for_local_live(
            Path(self.directory.name) / "state.sqlite"
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_initial_acceptance_is_one_off_then_schedule_allows_three_spaced_slots(self):
        first = datetime(2026, 11, 16, 10, 5, tzinfo=LOCAL)
        self.assertEqual(eligible_slot(first), None)
        self.assertEqual(reserve_source_slot(
            self.store, BING_REFRESH_STATE_KEY, first,
            error_code="BING_SCHEDULE_STATE_INVALID", allow_initial_acceptance=True,
        ), "INITIAL_ACCEPTANCE")
        self.assertIsNone(reserve_source_slot(
            self.store, BING_REFRESH_STATE_KEY,
            datetime(2026, 11, 16, 10, 6, tzinfo=LOCAL),
            error_code="BING_SCHEDULE_STATE_INVALID", allow_initial_acceptance=True,
        ))
        self.assertIsNone(eligible_slot(
            datetime(2026, 11, 16, 15, 5, tzinfo=LOCAL), last_attempt_at=first
        ))
        self.assertEqual(reserve_source_slot(
            self.store, BING_REFRESH_STATE_KEY,
            datetime(2026, 11, 16, 23, 5, tzinfo=LOCAL),
            error_code="BING_SCHEDULE_STATE_INVALID",
        ), "2026-11-16T23:00:00+07:00")
        self.assertEqual(reserve_source_slot(
            self.store, BING_REFRESH_STATE_KEY,
            datetime(2026, 11, 17, 7, 5, tzinfo=LOCAL),
            error_code="BING_SCHEDULE_STATE_INVALID",
        ), "2026-11-17T07:00:00+07:00")
        self.assertEqual(reserve_source_slot(
            self.store, BING_REFRESH_STATE_KEY,
            datetime(2026, 11, 17, 15, 5, tzinfo=LOCAL),
            error_code="BING_SCHEDULE_STATE_INVALID",
        ), "2026-11-17T15:00:00+07:00")

    def test_missed_slots_do_not_catch_up_and_metadata_reports_next_window(self):
        now = datetime(2026, 11, 16, 10, 11, tzinfo=LOCAL)
        self.assertIsNone(reserve_source_slot(
            self.store, BING_REFRESH_STATE_KEY, now,
            error_code="BING_SCHEDULE_STATE_INVALID",
        ))
        metadata = schedule_metadata(
            self.store, BING_REFRESH_STATE_KEY, now,
            error_code="BING_SCHEDULE_STATE_INVALID",
        )
        self.assertEqual(metadata["timezone"], "Asia/Ho_Chi_Minh")
        self.assertEqual(metadata["slots"], ["07:00", "15:00", "23:00"])
        self.assertEqual(metadata["max_attempts_per_day"], 3)
        self.assertEqual(metadata["next_eligible_at"], "2026-11-16T08:00:00+00:00")

    def test_schedule_state_corruption_fails_closed(self):
        self.store.transact(
            BING_REFRESH_STATE_KEY,
            lambda _state: ({"schema": 1, "last_attempt_at": "not-a-timestamp",
                             "last_slot": "slot"}, None),
        )
        with self.assertRaises(AdmissionDenied) as error:
            reserve_source_slot(
                self.store, BING_REFRESH_STATE_KEY,
                datetime(2026, 11, 16, 7, 5, tzinfo=LOCAL),
                error_code="BING_SCHEDULE_STATE_INVALID",
            )
        self.assertEqual(error.exception.code, "BING_SCHEDULE_STATE_INVALID")

    def test_utc_time_is_converted_to_the_configured_local_schedule(self):
        self.assertEqual(eligible_slot(datetime(2026, 11, 16, 0, 5, tzinfo=timezone.utc)),
                         "2026-11-16T07:00:00+07:00")

    def test_youtube_slots_are_user_triggered_and_enforce_eight_hour_spacing(self):
        first = datetime(2026, 11, 16, 7, 5, tzinfo=LOCAL)
        self.assertEqual(reserve_source_slot(
            self.store, YOUTUBE_REFRESH_STATE_KEY, first,
            error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
        ), "2026-11-16T07:00:00+07:00")
        self.assertIsNone(reserve_source_slot(
            self.store, YOUTUBE_REFRESH_STATE_KEY,
            datetime(2026, 11, 16, 15, 4, 59, tzinfo=LOCAL),
            error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
        ))
        self.assertEqual(reserve_source_slot(
            self.store, YOUTUBE_REFRESH_STATE_KEY,
            datetime(2026, 11, 16, 15, 5, tzinfo=LOCAL),
            error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
        ), "2026-11-16T15:00:00+07:00")
        self.assertEqual(reserve_source_slot(
            self.store, YOUTUBE_REFRESH_STATE_KEY,
            datetime(2026, 11, 16, 23, 5, tzinfo=LOCAL),
            error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
        ), "2026-11-16T23:00:00+07:00")
        metadata = schedule_metadata(
            self.store, YOUTUBE_REFRESH_STATE_KEY,
            datetime(2026, 11, 17, 7, 5, tzinfo=LOCAL),
            error_code="YOUTUBE_SCHEDULE_STATE_INVALID",
        )
        self.assertTrue(metadata["user_triggered_only"])
        self.assertEqual(metadata["slots"], ["07:00", "15:00", "23:00"])
        self.assertEqual(metadata["minimum_spacing_hours"], 8)

    def test_authenticated_manual_slot_ignores_clock_window_but_keeps_spacing_and_retry_identity(self):
        job_id = "a" * 32
        first = datetime(2026, 11, 16, 10, 11, tzinfo=LOCAL)
        self.assertEqual(manual_candidate_slot(
            self.store, BING_REFRESH_STATE_KEY, first,
            job_id=job_id, error_code="BING_SCHEDULE_STATE_INVALID",
        ), f"MANUAL:{job_id}")
        self.assertEqual(reserve_manual_source_slot(
            self.store, BING_REFRESH_STATE_KEY, first,
            job_id=job_id, error_code="BING_SCHEDULE_STATE_INVALID",
        ), f"MANUAL:{job_id}")
        # A retry of the same durable job remains idempotent inside the spacing window.
        self.assertEqual(reserve_manual_source_slot(
            self.store, BING_REFRESH_STATE_KEY,
            datetime(2026, 11, 16, 10, 12, tzinfo=LOCAL),
            job_id=job_id, error_code="BING_SCHEDULE_STATE_INVALID",
        ), f"MANUAL:{job_id}")
        other_job = "b" * 32
        retry_at = datetime(2026, 11, 16, 18, 10, 59, tzinfo=LOCAL)
        self.assertIsNone(manual_candidate_slot(
            self.store, BING_REFRESH_STATE_KEY, retry_at,
            job_id=other_job, error_code="BING_SCHEDULE_STATE_INVALID",
        ))
        self.assertEqual(manual_schedule_metadata(
            self.store, BING_REFRESH_STATE_KEY, retry_at,
            error_code="BING_SCHEDULE_STATE_INVALID",
        )["next_eligible_at"], "2026-11-16T11:11:00+00:00")
        self.assertEqual(reserve_manual_source_slot(
            self.store, BING_REFRESH_STATE_KEY,
            datetime(2026, 11, 16, 18, 11, tzinfo=LOCAL),
            job_id=other_job, error_code="BING_SCHEDULE_STATE_INVALID",
        ), f"MANUAL:{other_job}")


if __name__ == "__main__":
    unittest.main()
