import json
import sqlite3
import unittest
from types import SimpleNamespace

from app.api.capabilities import (
    _latest_saved_account_snapshot,
    _local_live_verification_status,
    _saved_source_activity,
)


class LocalLiveVerificationStatusTests(unittest.TestCase):
    def test_successful_local_live_fetch_verifies_google_and_bing(self):
        activity = {"status": "SUCCESS", "data_mode": "LIVE"}
        for source_id in ("google_web_serp", "bing_web_serp"):
            with self.subTest(source_id=source_id):
                source = SimpleNamespace(
                    source_id=source_id,
                    local_live_verification_status="NOT_VERIFIED",
                )
                self.assertEqual(
                    _local_live_verification_status(source, activity, True),
                    "LOCAL_LIVE_VERIFIED",
                )

    def test_failed_synthetic_or_non_live_runtime_keeps_catalog_status(self):
        source = SimpleNamespace(
            source_id="bing_web_serp",
            local_live_verification_status="NOT_VERIFIED",
        )
        for activity, runtime in (
            ({"status": "FAILED", "data_mode": "LIVE"}, True),
            ({"status": "SUCCESS", "data_mode": "SYNTHETIC"}, True),
            ({"status": "SUCCESS", "data_mode": "LIVE"}, False),
            (None, True),
        ):
            with self.subTest(activity=activity, runtime=runtime):
                self.assertEqual(
                    _local_live_verification_status(source, activity, runtime),
                    "NOT_VERIFIED",
                )

    def test_youtube_requires_fresh_live_provider_evidence_on_its_enabled_runtime(self):
        source = SimpleNamespace(
            source_id="youtube_video_search",
            local_live_verification_status="NOT_VERIFIED",
        )
        fresh_live = {
            "status": "SUCCESS",
            "data_mode": "LIVE",
            "delivery_state": "FRESH",
            "record_count": 3,
            "youtube_coverage": {
                "provider_method": "search.list",
                "provider_request_count": 1,
            },
        }
        self.assertEqual(
            _local_live_verification_status(source, fresh_live, True, True),
            "LOCAL_LIVE_VERIFIED",
        )

        for activity, youtube_runtime in (
            ({**fresh_live, "delivery_state": "CACHED"}, True),
            ({**fresh_live, "data_mode": "SYNTHETIC"}, True),
            ({**fresh_live, "youtube_coverage": {
                "provider_method": "search.list", "provider_request_count": 0,
            }}, True),
            ({**fresh_live, "record_count": 0}, True),
            (fresh_live, False),
        ):
            with self.subTest(activity=activity, youtube_runtime=youtube_runtime):
                self.assertEqual(
                    _local_live_verification_status(
                        source, activity, True, youtube_runtime
                    ),
                    "NOT_VERIFIED",
                )

    def test_saved_bing_activity_exposes_query_delivery_and_result_coverage(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE dataset_snapshots (status,data_mode,source_manifest,fetched_at,"
            "observation_start,observation_end,created_at)"
        )
        db.execute(
            "INSERT INTO dataset_snapshots VALUES (?,?,?,?,?,?,?)",
            (
                "COMPLETED",
                "LIVE",
                json.dumps([{
                    "source_id": "bing_web_serp",
                    "status": "SUCCESS",
                    "record_count": 1,
                    "delivery_state": "FRESH",
                    "coverage": {
                        "provider_request_count": 1,
                        "pages": 1,
                        "provider_result_count": 10,
                        "accepted_result_count": 10,
                        "rejected_result_count": 0,
                        "query": "fitness",
                        "discovery_mode": "SAVED_GOOGLE_CANDIDATE_BING_SERP",
                        "market": "US",
                        "language": "en-US",
                        "provider_cache_state": "UNKNOWN",
                        "provider_created_at": "2026-11-16T06:14:13+00:00",
                        "fetched_at": "2026-11-16T06:14:19+00:00",
                        "account_quota": {
                            "budget_status": "AVAILABLE",
                            "observed_at": "2026-11-16T06:14:13+00:00",
                            "monthly_limit": 250,
                            "monthly_remaining": 244,
                            "total_remaining": 244,
                            "month_to_date_searches": 6,
                            "current_hour_searches": 0,
                            "hourly_limit": 250,
                            "plan_renewal_date": "2026-12-11",
                        },
                        "candidate_source_id": "google_web_serp",
                        "candidate_selection_reason": "PREFERRED_INDUSTRY_SIGNAL",
                    },
                }]),
                "2026-11-16T06:14:19+00:00",
                None,
                None,
                "2026-11-16T06:14:19+00:00",
            ),
        )
        db.execute(
            "INSERT INTO dataset_snapshots VALUES (?,?,?,?,?,?,?)",
            (
                "COMPLETED",
                "LIVE",
                json.dumps([{
                    "source_id": "bing_web_serp",
                    "status": "SUCCESS",
                    "record_count": 1,
                    "delivery_state": "CACHED",
                    "coverage": {
                        "provider_request_count": 0,
                        "cache_state": "FRESH",
                        "cache_fetched_at": "2026-11-16T06:14:19+00:00",
                    },
                }]),
                "2026-11-16T06:34:01+00:00",
                None,
                None,
                "2026-11-16T06:34:01+00:00",
            ),
        )

        def recent_dataset_snapshots(*, limit):
            return db.execute(
                "SELECT * FROM dataset_snapshots ORDER BY fetched_at DESC LIMIT ?", (limit,)
            ).fetchall()

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            collection_service=SimpleNamespace(store=SimpleNamespace(
                data_mode_scope="LIVE", recent_dataset_snapshots=recent_dataset_snapshots,
            ))
        )))

        activity = _saved_source_activity(request)["bing_web_serp"]
        self.assertEqual(activity["data_mode"], "LIVE")
        self.assertEqual(activity["fetched_at"], "2026-11-16T06:14:19+00:00")
        self.assertEqual(activity["bing_coverage"]["query"], "fitness")
        self.assertEqual(activity["bing_coverage"]["accepted_result_count"], 10)
        self.assertEqual(activity["bing_coverage"]["delivery_state"], "CACHED")
        self.assertEqual(activity["bing_coverage"]["provider_cache_state"], "UNKNOWN")
        self.assertEqual(activity["bing_coverage"]["provider_request_count"], 0)
        self.assertEqual(
            _latest_saved_account_snapshot(
                _saved_source_activity(request)
            ),
            {
                "observed_at": "2026-11-16T06:14:13+00:00",
                "monthly_limit": 250,
                "monthly_remaining": 244,
                "total_remaining": 244,
                "month_to_date_searches": 6,
                "current_hour_searches": 0,
                "hourly_limit": 250,
                "plan_renewal_date": "2026-12-11",
            },
        )


if __name__ == "__main__":
    unittest.main()
