"""Offline checks for the one-shot D6.2 Wikimedia activation boundary."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.local_smoke_policy import local_smoke_policy
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from scripts.d6_wikimedia_live import (
    NoProviderCredentials,
    WikimediaOnlyApprovals,
    wikimedia_reservation_state,
)
from test_d5_public_admission import NOW


class WikimediaActivationTests(unittest.TestCase):
    def test_approval_is_limited_to_wikimedia_and_has_required_local_controls(self):
        provider = WikimediaOnlyApprovals(NOW)
        approval = provider.current(WIKIMEDIA_SOURCE_ID)
        self.assertEqual(approval.source_id, WIKIMEDIA_SOURCE_ID)
        self.assertEqual(approval.scope, "LOCAL_SMOKE")
        self.assertEqual(approval.approval_id, "D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12")
        for field in (
            "source_enabled", "global_enabled", "usage_approved", "retention_approved",
            "quota_approved", "cost_approved", "security_approved",
            "egress_controls_verified", "telemetry_controls_verified", "local_controls_verified",
        ):
            self.assertIs(getattr(approval, field), True, field)
        for source in (GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID):
            self.assertIsNone(provider.current(source))

    def test_no_provider_credentials_can_be_resolved(self):
        with self.assertRaisesRegex(RuntimeError, "unexpected credential resolution"):
            NoProviderCredentials().resolve("SERPAPI_API_KEY")

    def test_quota_preflight_reads_without_changing_or_resetting_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "ledger.sqlite"
            self.assertEqual(wikimedia_reservation_state(database), "NO_LEDGER")
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE atomic_state (state_key TEXT PRIMARY KEY, state_json TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO atomic_state VALUES (?, ?)",
                    ("d6-smoke", json.dumps({
                        "schema": 1,
                        "local_smoke_source_counts": {WIKIMEDIA_SOURCE_ID: 1},
                        "jobs": {"job": {"receipts": {}}},
                    })),
                )
                connection.commit()
            self.assertEqual(wikimedia_reservation_state(database), "RESERVED")
            with closing(sqlite3.connect(database)) as connection:
                stored = connection.execute(
                    "SELECT state_json FROM atomic_state WHERE state_key = ?", ("d6-smoke",)
                ).fetchone()[0]
            self.assertEqual(json.loads(stored)["local_smoke_source_counts"],
                             {WIKIMEDIA_SOURCE_ID: 1})

    def test_existing_receipt_is_detected_even_without_count_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "ledger.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE atomic_state (state_key TEXT PRIMARY KEY, state_json TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO atomic_state VALUES (?, ?)",
                    ("d6-smoke", json.dumps({
                        "schema": 1,
                        "jobs": {"job": {"receipts": {"receipt": {
                            "source_id": WIKIMEDIA_SOURCE_ID,
                        }}}},
                    })),
                )
                connection.commit()
            self.assertEqual(wikimedia_reservation_state(database), "RESERVED")

    def test_existing_smoke_policy_remains_one_attempt_and_one_start_per_day(self):
        policy = local_smoke_policy()
        self.assertEqual(policy.pools["wikimedia"].daily_requests, 1)
        self.assertEqual(policy.pools["wikimedia"].monthly_requests, 1)
        self.assertEqual(policy.max_starts_per_window, 1)
        self.assertEqual(policy.window_seconds, 86_400)


if __name__ == "__main__":
    unittest.main()
