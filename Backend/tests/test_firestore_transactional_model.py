"""Offline serialization contracts for the Firestore transaction adapter."""

from datetime import datetime, timedelta, timezone
import unittest

from app.jobs.contracts import Job, Policy, State
from app.jobs.fake import _identity as fake_identity
from app.jobs.firestore_transactional import (
    MAX_SHARD_DOCUMENT_JSON_BYTES,
    _identity,
    _job_from,
    _job_record,
    _validate_shard_payload,
)
from app.storage_analysis.planning import Estimate
from app.workers.safety import OfflineBudget


class FirestoreTransactionalModelTests(unittest.TestCase):
    def payload(self):
        return {
            "job_type": "analysis",
            "source": "wikimedia_pageviews_top_per_country",
            "scope": {"country": "US", "access": "all-access", "project": "all"},
            "resource_id": "synthetic-resource",
            "window": 7,
            "config_version": "test-v1",
            "occurrence_id": "test-occurrence",
            "tenant_scope": "synthetic",
            "dataset_config": {
                "analysis_date": "2026-01-08", "window_start": "2026-01-01", "window_end": "2026-01-08",
                "source_version": "wikimedia-top-per-country-v1", "processing_version": "cleaning-v1",
                "cleaning_version": "cleaning-v1", "classification_version": "rules-v1",
                "algorithm_version": "score-v1", "provenance": "https://example.invalid/source",
            },
        }

    def policy(self):
        return Policy(3, 10, 2, datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=1),
                      30, 10, 2, Estimate(reads=5), Estimate(reads=10),
                      operation_budget=OfflineBudget(100, 100))

    def test_canonical_identity_digest_matches_fake_reference(self):
        payload = self.payload()
        self.assertEqual(_identity(payload), fake_identity(payload))

    def test_job_and_nested_policy_budget_round_trip(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = Job("job-id", "idem", "digest", "v1", State.QUEUED, 4, now, now,
                  self.policy(), owner="worker", fencing=3, expires_at=now + timedelta(seconds=30),
                  heartbeat_at=now, attempts=2, requests_used=1, retries_used=1,
                  retry_after=now + timedelta(seconds=4), reason_code="RETRYING",
                  recovery_reserved=True, identity=self.payload(),
                  audit=[{"revision": 4, "state": "QUEUED", "action": "CLAIMED", "at": now, "reason_code": None}])
        job.refresh_budget = OfflineBudget(100, 100, reads=12, writes=8, metadata_operations=5,
                                           heartbeats=2, source_requests=3, retry_operations=1)
        restored = _job_from(_job_record(job))
        self.assertEqual(restored, job)

    def test_shard_size_preflight_rejects_oversize_before_firestore(self):
        _validate_shard_payload({"items": ["á"]})
        with self.assertRaisesRegex(ValueError, "partition it"):
            _validate_shard_payload({"items": ["x" * MAX_SHARD_DOCUMENT_JSON_BYTES]})

    def test_shard_preflight_rejects_non_finite_json_values(self):
        with self.assertRaisesRegex(ValueError, "finite JSON-compatible"):
            _validate_shard_payload({"value": float("nan")})


if __name__ == "__main__":
    unittest.main()
