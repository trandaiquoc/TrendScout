"""Sanitized, read-only production-ledger preflight contracts."""

import unittest

from app.google_live_settings import GoogleLiveSettings
from app.jobs.public_admission import PublicAdmission
from app.normal_wikimedia_live_policy import GOOGLE_STATE_KEY
from scripts.d6_firestore_policy_preflight import (
    _policies_for_preflight,
    classify_failure,
    failure_report,
    summarize_ledger,
)


class FirestorePolicyPreflightTests(unittest.TestCase):
    def test_reports_target_and_recognized_previous_policy_without_ledger_values(self):
        settings = GoogleLiveSettings(
            bing_approved=True,
            account_api_approved=True,
        )
        policies = _policies_for_preflight(settings)
        previous = policies["previous_without_bing"]
        previous_digest = PublicAdmission._policy_digest(previous)
        state = {
            "schema": 1,
            "policy_digest": previous_digest,
            "operational_enabled": True,
            "jobs": {"private-job-id": {"state": "FAILED"}},
            "keys": {"private-key": {"job_id": "private-job-id"}},
            "starts": ["2026-11-19T00:00:00+00:00"],
            "quota": {"serpapi:day:2026-11-19": {"requests": 2, "cost": 2}},
            "inflight": {},
            "policy_migration_audit": [],
        }

        report = summarize_ledger(state, policies)

        self.assertEqual(report["state_key"], GOOGLE_STATE_KEY)
        self.assertFalse(report["matches_windows2_current_policy"])
        self.assertEqual(report["recognized_previous_policy_labels"], ["previous_without_bing"])
        self.assertEqual(report["jobs_count"], 1)
        self.assertEqual(report["idempotency_keys_count"], 1)
        self.assertEqual(report["starts_count"], 1)
        self.assertEqual(report["quota_counter_count"], 1)
        self.assertNotIn("private-job-id", str(report))
        self.assertNotIn("private-key", str(report))
        self.assertNotIn("requests", str(report))

    def test_unknown_digest_stays_unclassified_and_current_digest_is_explicit(self):
        policies = _policies_for_preflight(GoogleLiveSettings())
        report = summarize_ledger(
            {"schema": 1, "policy_digest": "f" * 64, "jobs": {}, "keys": {},
             "starts": [], "quota": {}, "inflight": {}},
            policies,
        )

        self.assertEqual(report["recognized_previous_policy_labels"], [])
        self.assertFalse(report["matches_windows2_current_policy"])
        self.assertEqual(
            report["windows2_current_policy_digest"],
            PublicAdmission._policy_digest(policies["windows2_current"]),
        )

    def test_impersonation_failure_is_classified_without_leaking_error_detail(self):
        error = RuntimeError(
            "Permission 'iam.serviceAccounts.getAccessToken' denied; "
            "sensitive-provider-detail"
        )
        report = failure_report(
            stage="ADC_IMPERSONATION", code=classify_failure(error)
        )

        self.assertEqual(report["failure_code"], "SERVICE_ACCOUNT_IMPERSONATION_DENIED")
        self.assertEqual(report["failure_stage"], "ADC_IMPERSONATION")
        self.assertFalse(report["firestore_read_attempted"])
        self.assertFalse(report["firestore_state_read"])
        self.assertFalse(report["firestore_state_write"])
        self.assertEqual(report["provider_calls"], 0)
        self.assertNotIn("sensitive-provider-detail", str(report))

    def test_unrecognized_failure_uses_generic_sanitized_reason(self):
        report = failure_report(
            stage="FIRESTORE_READ", code=classify_failure(RuntimeError("private detail"))
        )

        self.assertEqual(report["failure_code"], "FIRESTORE_PREFLIGHT_FAILED")
        self.assertNotIn("private detail", str(report))


if __name__ == "__main__":
    unittest.main()
