"""Offline tests for fenced, policy-scoped source checkpoints."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from test_d5_public_admission import NOW, coordinator
from test_d5_public_admission import policy as admission_policy

from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant, SourceCheckpointJournal
from app.sources.catalog import GOOGLE_SOURCE_ID


def grant(*, seconds=600, allowed=True, mode="SYNTHETIC", version="policy-v1"):
    return CheckpointGrant(GOOGLE_SOURCE_ID, mode, version, NOW + timedelta(seconds=seconds),
                           300, allowed)


class SourceCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "coordinator.sqlite")
        self.admission = coordinator(self.path)
        self.job = self.admission.admit("checkpoint-idempotency-key", now=NOW)["job_id"]
        self.owner = "worker"
        self.fence = self.admission.claim(self.job, self.owner, now=NOW)["fence"]
        self.journal = SourceCheckpointJournal(self.admission)
        self.snapshot = {"schema": 1, "source_id": GOOGLE_SOURCE_ID,
                         "data_mode": "SYNTHETIC",
                         "plan_fingerprint": self.admission.policy.plan_fingerprint,
                         "cursor": 2, "records": [{"title": "demo", "count": 4}]}

    def tearDown(self):
        self.directory.cleanup()

    def save(self, *, expected=0, value=None, policy=None, at=NOW):
        return self.journal.save(self.job, self.owner, self.fence, source_id=GOOGLE_SOURCE_ID,
                                 grant=policy or grant(), now=at, expected_revision=expected,
                                 snapshot=value or self.snapshot)

    def load(self, *, policy=None, at=NOW):
        return self.journal.load(self.job, self.owner, self.fence, source_id=GOOGLE_SOURCE_ID,
                                 grant=policy or grant(), now=at)

    def test_save_load_and_idempotent_retry(self):
        self.assertEqual(self.save(), 1)
        self.assertEqual(self.save(), 1)
        loaded = self.load()
        self.assertEqual(loaded["revision"], 1)
        self.assertEqual(loaded["snapshot"], self.snapshot)

    def test_revision_conflict_and_expired_or_revoked_grants_fail_closed(self):
        self.save()
        changed = {**self.snapshot, "cursor": 3}
        with self.assertRaisesRegex(AdmissionDenied, "CHECKPOINT_REVISION_CONFLICT"):
            self.save(value=changed)
        with self.assertRaisesRegex(AdmissionDenied, "CHECKPOINT_POLICY_UNAVAILABLE"):
            self.load(policy=grant(allowed=False))
        with self.assertRaisesRegex(AdmissionDenied, "CHECKPOINT_POLICY_UNAVAILABLE"):
            self.load(policy=grant(seconds=0), at=NOW)

    def test_idempotent_retry_tightens_retention(self):
        self.save()
        tightened = grant(seconds=10)
        self.assertEqual(self.save(policy=tightened), 1)
        self.admission.heartbeat(self.job, self.owner, self.fence,
                                 now=NOW + timedelta(seconds=9))
        self.assertIsNone(self.load(at=NOW + timedelta(seconds=11)))

    def test_scope_and_sensitive_data_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid checkpoint scope"):
            self.save(value={**self.snapshot, "source_id": "other"})
        with self.assertRaises(ValueError):
            self.save(value={**self.snapshot, "records": [{"api_key": "private"}]})
        with self.assertRaises(ValueError):
            self.save(value={**self.snapshot, "records": [{"value": float("nan")}]})

    def test_snapshot_size_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "storage budget"):
            self.save(value={**self.snapshot, "padding": "x" * 160_000})

    def test_stale_fence_cannot_read_or_write_after_reclaim(self):
        self.save()
        successor = self.admission.claim(self.job, "worker2", now=NOW + timedelta(seconds=11))
        with self.assertRaisesRegex(AdmissionDenied, "LEASE_LOST"):
            self.load(at=NOW + timedelta(seconds=11))
        with self.assertRaisesRegex(AdmissionDenied, "LEASE_LOST"):
            self.journal.save(self.job, self.owner, self.fence, source_id=GOOGLE_SOURCE_ID,
                              grant=grant(), now=NOW + timedelta(seconds=11),
                              expected_revision=1, snapshot=self.snapshot)
        new_journal = SourceCheckpointJournal(coordinator(self.path))
        loaded = new_journal.load(self.job, "worker2", successor["fence"],
                                  source_id=GOOGLE_SOURCE_ID, grant=grant(),
                                  now=NOW + timedelta(seconds=11))
        self.assertEqual(loaded["revision"], 1)

    def test_corrupt_stored_snapshot_is_rejected(self):
        self.save()
        def corrupt(state):
            state["jobs"][self.job]["source_checkpoints"][GOOGLE_SOURCE_ID]["snapshot"]["cursor"] = 9
            return state, None
        self.admission.store.transact("public", corrupt)
        with self.assertRaisesRegex(AdmissionDenied, "CHECKPOINT_INVALID"):
            self.load()

    def test_maintenance_prunes_payload_before_replay_receipt_and_runs_when_disabled(self):
        short_policy = admission_policy(
            lease_seconds=10, max_job_seconds=10, receipt_seconds=20,
        )
        admission = PublicAdmission(
            self.admission.store, short_policy, state_key="retention"
        )
        job_id = admission.admit("retention-maintenance-key", now=NOW)["job_id"]
        fence = admission.claim(job_id, "worker", now=NOW)["fence"]
        journal = SourceCheckpointJournal(admission)
        journal.save(
            job_id, "worker", fence, source_id=GOOGLE_SOURCE_ID, grant=grant(),
            now=NOW, expected_revision=0,
            snapshot={"schema": 1, "source_id": GOOGLE_SOURCE_ID,
                      "data_mode": "SYNTHETIC",
                      "plan_fingerprint": admission.policy.plan_fingerprint,
                      "records": [{"title": "short-lived fixture"}]},
        )
        admission.disable()

        first = admission.prune_expired(now=NOW + timedelta(seconds=11))
        retained = admission.store.transact(
            admission.state_key,
            lambda state: (state, state["jobs"][job_id]),
        )
        self.assertEqual(first["source_checkpoints"], 1)
        self.assertEqual(retained["state"], "EXPIRED")
        self.assertEqual(retained["source_checkpoints"], {})
        self.assertEqual(retained["job_id"], job_id)

        second = admission.prune_expired(now=NOW + timedelta(seconds=21))
        remaining = admission.store.transact(
            admission.state_key,
            lambda state: (state, (job_id in state["jobs"], bool(state["keys"]))),
        )
        self.assertEqual(second["idempotency_receipts"], 1)
        self.assertEqual(second["jobs"], 1)
        self.assertEqual(remaining, (False, False))


if __name__ == "__main__":
    unittest.main()
