"""Offline regressions for retired D6.2 activation and launcher diagnostics."""

import asyncio
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_d5_public_admission import NOW

from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from scripts.d6_wikimedia_live import (
    LEGACY_APPROVAL_BLOCKED,
    LEGACY_APPROVAL_ID,
    NoProviderCredentials,
    WikimediaOnlyApprovals,
    _run_once,
    _summary,
    legacy_approval_preflight,
    main,
    wikimedia_reservation_state,
)


class WikimediaActivationTests(unittest.TestCase):
    def test_historical_approval_is_retired_for_every_source_and_expiry(self):
        for expiry in (NOW - timedelta(days=1), NOW, NOW + timedelta(days=3650)):
            provider = WikimediaOnlyApprovals(expiry)
            self.assertIsNone(provider.current(WIKIMEDIA_SOURCE_ID))
            self.assertIsNone(provider.current("google_trends"))
            result = legacy_approval_preflight(
                approval_id=LEGACY_APPROVAL_ID, expires_at=expiry,
            )
            self.assertEqual(result["reason"], LEGACY_APPROVAL_BLOCKED)
            self.assertEqual(result["provider_request_count"], 0)

    def test_old_cli_flag_and_new_process_cannot_activate_or_start_app(self):
        output = io.StringIO()
        with patch("app.main.create_local_live_app", side_effect=AssertionError("app startup"),
                   create=True), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--owner-approved-wikimedia-only"]), 1)
            result = asyncio.run(_run_once())
        self.assertEqual(json.loads(output.getvalue())["reason"], LEGACY_APPROVAL_BLOCKED)
        self.assertEqual(result["reason"], LEGACY_APPROVAL_BLOCKED)
        self.assertEqual(result["provider_request_count"], 0)

    def test_empty_and_consumed_temporary_ledgers_never_change_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty.sqlite"
            self.assertEqual(wikimedia_reservation_state(empty), "NO_LEDGER")
            self.assertEqual(legacy_approval_preflight()["status"], "BLOCKED")

            consumed = Path(directory) / "consumed.sqlite"
            with closing(sqlite3.connect(consumed)) as connection:
                connection.execute(
                    "CREATE TABLE atomic_state (state_key TEXT PRIMARY KEY, state_json TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO atomic_state VALUES (?, ?)",
                    ("d6-smoke", json.dumps({
                        "schema": 1,
                        "local_smoke_source_counts": {WIKIMEDIA_SOURCE_ID: 1},
                        "jobs": {"job": {"receipts": {"receipt": {
                            "source_id": WIKIMEDIA_SOURCE_ID,
                        }}}},
                    })),
                )
                connection.commit()
            before = consumed.read_bytes()
            self.assertEqual(wikimedia_reservation_state(consumed), "RESERVED")
            self.assertEqual(legacy_approval_preflight()["status"], "BLOCKED")
            self.assertEqual(consumed.read_bytes(), before)

    def test_missing_or_corrupt_ledger_state_is_not_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory) / "corrupt.sqlite"
            with closing(sqlite3.connect(corrupt)) as connection:
                connection.execute("CREATE TABLE unrelated (value TEXT)")
            self.assertEqual(wikimedia_reservation_state(corrupt), "INVALID_LEDGER")
            self.assertEqual(legacy_approval_preflight()["status"], "BLOCKED")

    def test_diagnostic_whitelist_preserves_failures_without_inventing_success(self):
        cases = (
            ("BODY_STREAM", "ReadError", "UPSTREAM_ERROR", 200),
            ("BODY_STREAM", "RemoteProtocolError", "UPSTREAM_ERROR", 200),
            ("BODY_STREAM", "ReadTimeout", "UPSTREAM_TIMEOUT", 200),
            ("REQUEST_DISPATCH", "HttpDeadlineExceeded", "UPSTREAM_TIMEOUT", None),
            ("JSON_DECODE", "JSONDecodeError", "MALFORMED_RESPONSE", 200),
            ("SCHEMA_VALIDATION", "ValueError", "MALFORMED_RESPONSE", 200),
            ("BODY_STREAM", None, "RESPONSE_TOO_LARGE", 200),
        )
        rows = []
        for index, (phase, exception, code, status) in enumerate(cases):
            rows.append({
                "source_id": WIKIMEDIA_SOURCE_ID,
                "status": "FAILED",
                "data_mode": "LIVE",
                "record_count": 0,
                "error_code": code,
                "transport_diagnostic": {
                    "source_id": WIKIMEDIA_SOURCE_ID,
                    "failure_phase": phase,
                    "http_status": status,
                    "exception_class": exception,
                    "bytes_received": 4096 if code != "RESPONSE_TOO_LARGE" else 1_000_001,
                    "response_size_limit": 1_000_000,
                    "attempt_id": f"attempt_{index}",
                    "error_code": code,
                },
            })
        status_payload = {"status": "PARTIAL", "data_mode": "LIVE", "sources": rows}
        summary = _summary(status_payload)
        self.assertEqual(_summary(status_payload), summary)  # repeated status polling is stable
        self.assertEqual(len(summary["sources"]), len(cases))
        for row, original in zip(summary["sources"], rows):
            self.assertEqual(row["status"], "FAILED")
            self.assertEqual(row["error_code"], original["error_code"])
            self.assertEqual(row["transport_diagnostic"], original["transport_diagnostic"])
        self.assertEqual(summary["sources"][0]["transport_diagnostic"]["http_status"], 200)

    def test_missing_and_malformed_diagnostics_are_omitted(self):
        valid = {
            "source_id": WIKIMEDIA_SOURCE_ID,
            "failure_phase": "BODY_STREAM",
            "http_status": 200,
            "exception_class": "ReadError",
            "bytes_received": 128,
            "response_size_limit": 1_000_000,
            "attempt_id": "attempt_safe_1",
            "error_code": "UPSTREAM_ERROR",
        }
        malformed = [
            {**valid, "http_status": True},
            {**valid, "bytes_received": "128"},
            {**valid, "exception_class": "ReadError: secret path"},
            {**valid, "error_code": "UPSTREAM_TIMEOUT"},
            {**valid, "extra": "must not pass"},
            {**valid, "raw_body": "RAW_RESPONSE_SENTINEL"},
            {**valid, "attempt_id": "../../private"},
        ]
        rows = [{"source_id": WIKIMEDIA_SOURCE_ID, "status": "FAILED"}]
        rows.extend({"source_id": WIKIMEDIA_SOURCE_ID, "status": "FAILED",
                     "transport_diagnostic": diagnostic} for diagnostic in malformed)
        summary = _summary({"status": "PARTIAL", "sources": rows})
        self.assertTrue(all("transport_diagnostic" not in row for row in summary["sources"]))
        encoded = json.dumps(summary)
        self.assertNotIn("RAW_RESPONSE_SENTINEL", encoded)
        self.assertNotIn("private", encoded)

    def test_no_provider_credentials_can_be_resolved(self):
        with self.assertRaisesRegex(RuntimeError, "credential resolution is disabled"):
            NoProviderCredentials().resolve("SERPAPI_API_KEY")


if __name__ == "__main__":
    unittest.main()
