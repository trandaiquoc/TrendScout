"""Offline tests for fenced, policy-scoped source checkpoints."""

import tempfile
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from test_d5_public_admission import NOW, coordinator
from test_d5_public_admission import policy as admission_policy

from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant, SourceCheckpointJournal
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID
from app.storage.firestore_atomic_state import CommitOutcomeUnknown


def grant(*, source_id=GOOGLE_SOURCE_ID, seconds=600, allowed=True,
          mode="SYNTHETIC", version="policy-v1"):
    return CheckpointGrant(source_id, mode, version, NOW + timedelta(seconds=seconds),
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

    def test_save_many_commits_multiple_source_receipts_in_one_owned_transaction(self):
        checkpoints = []
        for source_id in (GOOGLE_SOURCE_ID, BING_SOURCE_ID):
            result = {
                "status": "SUCCESS",
                "data_mode": "SYNTHETIC",
                "record_count": 1,
                "records": [{"evidence_id": source_id}],
            }
            checkpoints.append({
                "source_id": source_id,
                "grant": grant(source_id=source_id),
                "expected_revision": 0,
                "snapshot": {
                    "schema": 1,
                    "source_id": source_id,
                    "data_mode": "SYNTHETIC",
                    "plan_fingerprint": self.admission.policy.plan_fingerprint,
                    "phase": "COMPLETE",
                    "result": result,
                },
            })

        with patch.object(self.admission, "transact_owned",
                          wraps=self.admission.transact_owned) as transact_owned:
            revisions = self.journal.save_many(
                self.job, self.owner, self.fence, now=NOW,
                checkpoints=checkpoints,
            )

        self.assertEqual(revisions, {GOOGLE_SOURCE_ID: 1, BING_SOURCE_ID: 1})
        self.assertEqual(transact_owned.call_count, 1)
        for source_id in (GOOGLE_SOURCE_ID, BING_SOURCE_ID):
            loaded = self.journal.load(
                self.job, self.owner, self.fence, source_id=source_id,
                grant=grant(source_id=source_id), now=NOW,
            )
            self.assertEqual(loaded["revision"], 1)
            self.assertEqual(loaded["snapshot"]["result"]["records"], [
                {"evidence_id": source_id},
            ])

    def test_save_many_uploads_independent_record_blobs_concurrently(self):
        barrier = Barrier(2)

        class SnapshotBlobs:
            def put(self, records, *, now, retain_until):
                barrier.wait(timeout=2)
                source_id = records[0]["source_id"]
                return {"digest": ("a" if source_id == GOOGLE_SOURCE_ID else "b") * 64,
                        "raw_bytes": 170_000, "item_count": len(records)}

        journal = SourceCheckpointJournal(self.admission, snapshot_blobs=SnapshotBlobs())
        checkpoints = []
        for source_id in (GOOGLE_SOURCE_ID, BING_SOURCE_ID):
            result = {
                "status": "SUCCESS", "data_mode": "SYNTHETIC",
                "record_count": 1,
                "records": [{"source_id": source_id, "description": "x" * 170_000}],
            }
            checkpoints.append({
                "source_id": source_id,
                "grant": grant(source_id=source_id),
                "expected_revision": 0,
                "snapshot": {
                    "schema": 1, "source_id": source_id,
                    "data_mode": "SYNTHETIC",
                    "plan_fingerprint": self.admission.policy.plan_fingerprint,
                    "phase": "COMPLETE", "result": result,
                },
            })

        revisions = journal.save_many(
            self.job, self.owner, self.fence, now=NOW, checkpoints=checkpoints,
        )

        self.assertEqual(revisions, {GOOGLE_SOURCE_ID: 1, BING_SOURCE_ID: 1})

    def test_large_collector_progress_records_use_existing_snapshot_blob_store(self):
        class SnapshotBlobs:
            def __init__(self):
                self.records = None

            def put(self, records, *, now, retain_until):
                self.records = deepcopy(records)
                return {"digest": "a" * 64, "raw_bytes": 200_000,
                        "item_count": len(records)}

            def get(self, _reference, *, now):
                return deepcopy(self.records)

        blobs = SnapshotBlobs()
        journal = SourceCheckpointJournal(self.admission, snapshot_blobs=blobs)
        collector_records = [{"video_id": "video-001", "description": "x" * 180_000}]
        snapshot = {
            "schema": 1, "source_id": GOOGLE_SOURCE_ID, "data_mode": "SYNTHETIC",
            "plan_fingerprint": self.admission.policy.plan_fingerprint,
            "phase": "RUNNING", "progress": {"collector": {"records": collector_records}},
        }
        self.assertEqual(journal.save(
            self.job, self.owner, self.fence, source_id=GOOGLE_SOURCE_ID,
            grant=grant(), now=NOW, expected_revision=0, snapshot=snapshot,
        ), 1)
        stored = self.admission.store.transact(
            "public", lambda state: (state, state["jobs"][self.job]["source_checkpoints"][GOOGLE_SOURCE_ID]),
        )
        self.assertEqual(stored["snapshot_storage"], "EXTERNAL_RECORDS")
        self.assertEqual(stored["snapshot_ref"]["path"], ["progress", "collector", "records"])
        restored = journal.load(
            self.job, self.owner, self.fence, source_id=GOOGLE_SOURCE_ID,
            grant=grant(), now=NOW,
        )
        self.assertEqual(restored["snapshot"]["progress"]["collector"]["records"],
                         collector_records)

    def test_unknown_checkpoint_commit_is_reconciled_without_losing_receipt(self):
        underlying = self.admission.store

        class LostAcknowledgementStore:
            def __init__(self):
                self.uncertain = True
                self.calls = 0

            def transact(self, key, operation):
                self.calls += 1
                result = underlying.transact(key, operation)
                if self.uncertain:
                    self.uncertain = False
                    raise CommitOutcomeUnknown("synthetic lost commit acknowledgement")
                return result

        store = LostAcknowledgementStore()
        self.admission.store = store
        self.journal = SourceCheckpointJournal(self.admission)

        self.assertEqual(self.save(), 1)
        self.assertEqual(store.calls, 2)
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
        self.assertEqual(first["source_checkpoints"], 0)
        self.assertEqual(retained["state"], "EXPIRED")
        self.assertIn(GOOGLE_SOURCE_ID, retained["source_checkpoints"])
        self.assertEqual(retained["job_id"], job_id)

        second = admission.prune_expired(now=NOW + timedelta(seconds=21))
        remaining = admission.store.transact(
            admission.state_key,
            lambda state: (state, (job_id in state["jobs"], bool(state["keys"]),
                                   GOOGLE_SOURCE_ID in state["jobs"][job_id]["source_checkpoints"])),
        )
        self.assertEqual(second["idempotency_receipts"], 1)
        self.assertEqual(second["jobs"], 0)
        self.assertEqual(remaining, (True, False, True))

        third = admission.prune_expired(now=NOW + timedelta(seconds=301))
        final = admission.store.transact(
            admission.state_key,
            lambda state: (state, (job_id in state["jobs"], bool(state["keys"]))),
        )
        self.assertEqual(third["source_checkpoints"], 1)
        self.assertEqual(third["jobs"], 1)
        self.assertEqual(final, (False, False))

    def test_deadline_terminalizes_saved_source_as_partial_without_dropping_its_result(self):
        long_grant = CheckpointGrant(
            GOOGLE_SOURCE_ID, "SYNTHETIC", "policy-v1", NOW + timedelta(seconds=600),
            600, True,
        )
        complete = {
            "schema": 1,
            "source_id": GOOGLE_SOURCE_ID,
            "data_mode": "SYNTHETIC",
            "plan_fingerprint": self.admission.policy.plan_fingerprint,
            "phase": "COMPLETE",
            "result": {
                "status": "SUCCESS", "data_mode": "SYNTHETIC",
                "records": [{"title": "saved Google result"}], "record_count": 1,
            },
        }
        self.journal.save(
            self.job, self.owner, self.fence, source_id=GOOGLE_SOURCE_ID,
            grant=long_grant, now=NOW, expected_revision=0, snapshot=complete,
        )

        status = self.admission.read_job(self.job, now=NOW + timedelta(seconds=301))

        self.assertEqual(status["state"], "PARTIAL")
        self.assertIsNotNone(status["finished_at"])
        self.assertIn(GOOGLE_SOURCE_ID, status["source_checkpoints"])


if __name__ == "__main__":
    unittest.main()
