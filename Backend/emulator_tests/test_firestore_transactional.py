"""Real Firestore Emulator integration tests; runner must provide sandboxed host."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
import unittest
import uuid
from unittest.mock import patch

from google.api_core.exceptions import Aborted, ServiceUnavailable
from google.cloud.firestore_v1.transaction import Transaction

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "Backend"
sys.path.insert(0, str(BACKEND / "tests"))

from app.datasets.manifest import build_offline_manifest
from app.datasets.fake import digest
from app.jobs.contracts import Policy, State, Conflict, DeadlineExceeded, LeaseLost
from app.jobs.firestore_transactional import (
    FirestoreTransactionalCoordinator,
    FirestoreTransactionalDatasetStore,
    TransactionOutcomeUnknown,
    _doc_id,
)
from app.storage.firestore_emulator import create_guarded_emulator_client
from app.storage_analysis.planning import Estimate
from app.workers.safety import OfflineBudget
from app.workers.safety import ErrorCode, RefreshError
from refresh_fixtures import strict_output


class EmulatorClock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FirestoreTransactionalEmulatorTests(unittest.TestCase):
    """Each test writes only synthetic, uniquely named documents and removes them."""

    def setUp(self):
        self.client = create_guarded_emulator_client()
        self.clients = [self.client]
        self.clock = EmulatorClock()
        self.suffix = uuid.uuid4().hex
        self.idempotency_key = f"p3s1-emulator-{self.suffix}"
        self.occurrence = f"occurrence-{self.suffix}"
        self.resource = f"resource-{self.suffix}"
        self.dataset_ids = []
        self.jobs = FirestoreTransactionalCoordinator(self.client, clock=self.clock)
        self.datasets = FirestoreTransactionalDatasetStore(self.client, clock=self.clock)
        self.policy = Policy(
            max_attempts=4,
            max_requests=10,
            retry_budget=2,
            deadline=self.clock.now + timedelta(hours=1),
            lease_seconds=30,
            heartbeat_seconds=10,
            backoff_seconds=1,
            estimate=Estimate(),
            budget=Estimate(),
            operation_budget=OfflineBudget(20000, 20000),
        )

    def tearDown(self):
        for dataset_id in self.dataset_ids:
            dataset_ref = self.client.collection("_ts3_datasets").document(dataset_id)
            for collection in ("shards", "meta"):
                for snapshot in dataset_ref.collection(collection).stream():
                    snapshot.reference.delete()
            dataset_ref.delete()
        self.client.collection("_ts3_idempotency").document(_doc_id(self.idempotency_key)).delete()
        self.client.collection("_ts3_occurrences").document(_doc_id(self.occurrence)).delete()
        self.client.collection("_ts3_jobs").document(self.job_id).delete() if hasattr(self, "job_id") else None
        self.client.collection("_ts3_resources").document(_doc_id(self.resource)).delete()
        for client in self.clients:
            client.close()

    def payload(self, occurrence=None, resource=None):
        occurrence = occurrence or self.occurrence
        resource = resource or self.resource
        return {
            "job_type": "analysis",
            "source": "wikimedia_pageviews_top_per_country",
            "scope": {"country": "US", "access": "all-access", "project": "all"},
            "resource_id": resource,
            "window": 7,
            "config_version": "emulator-v1",
            "occurrence_id": occurrence,
            "tenant_scope": "synthetic-emulator-test",
            "dataset_config": {
                "analysis_date": "2026-01-08",
                "window_start": "2026-01-01",
                "window_end": "2026-01-08",
                "source_version": "wikimedia-top-per-country-v1",
                "processing_version": "cleaning-pipeline-v0.1",
                "cleaning_version": "cleaning-pipeline-v0.1",
                "classification_version": "title-rules-v0.1",
                "algorithm_version": "wikimedia-trend-v0.1",
                "provenance": "https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/US/all-access/2026/01/08",
            },
        }

    def create_job(self, key=None, occurrence=None, resource=None):
        key = key or self.idempotency_key
        payload = self.payload(occurrence, resource)
        job = self.jobs.create(key, payload, self.policy)
        self.job_id = job.job_id
        return job

    def claim_job(self, jobs=None, job_id=None, owner="worker"):
        coordinator = jobs or self.jobs
        current = coordinator.claim(job_id or self.job_id, owner)
        token = self.datasets.claim_resource(self.resource, current.job_id, owner, current.fencing, current.revision)
        current = coordinator.get(current.job_id)
        return current, token

    def prepare_ready_dataset(self, dataset_id, *, owner="worker", job_id=None, token=None):
        job_id = job_id or self.job_id
        self.dataset_ids.append(dataset_id)
        job = self.jobs.get(job_id)
        if token is None:
            if job.state == State.QUEUED:
                job, token = self.claim_job(job_id=job_id, owner=owner)
        if job.state != State.VALIDATING:
            for state in (State.FETCHING, State.NORMALIZING, State.ANALYZING, State.PERSISTING):
                job = self.jobs.transition(job_id, owner, job.fencing, job.revision, state)
        output = strict_output({"shards": {"fixture-v1": {"items": ["synthetic"]}}, "quality_ok": True})
        metadata = dict(output["manifest_input"], occurrence_id=job.identity["occurrence_id"])
        self.datasets.draft(dataset_id, self.resource, job_id, owner, job.fencing, job.revision, token, metadata)
        for shard_id, shard in output["shards"].items():
            self.datasets.put_shard(dataset_id, self.resource, job_id, owner, job.fencing,
                                    job.revision, token, shard_id, shard)
        report = dict(output["quality_report"], dataset_id=dataset_id)
        self.datasets.put_quality_report(dataset_id, self.resource, job_id, owner, job.fencing,
                                         job.revision, token, report)
        manifest = build_offline_manifest(
            dataset_id=dataset_id,
            job_id=job_id,
            resource_id=self.resource,
            identity=job.identity,
            metadata=metadata,
            shards=output["shards"],
            quality_report=report,
            hash_fn=digest,
        )
        self.datasets.seal(dataset_id, self.resource, job_id, owner, job.fencing,
                           job.revision, token, manifest)
        job = self.jobs.transition(job_id, owner, job.fencing, job.revision, State.VALIDATING)
        self.assertTrue(self.datasets.verify(dataset_id, self.resource, job_id, owner,
                                             job.fencing, job.revision, token))
        job = self.jobs.get(job_id)
        return job, token

    def test_occurrence_create_is_idempotent_across_independent_clients(self):
        first = self.create_job()
        second_client = create_guarded_emulator_client()
        self.clients.append(second_client)
        second = FirestoreTransactionalCoordinator(second_client, clock=self.clock).create(
            self.idempotency_key, self.payload(), self.policy
        )
        self.assertEqual(second.job_id, first.job_id)
        with self.assertRaises(Conflict):
            FirestoreTransactionalCoordinator(second_client, clock=self.clock).create(
                f"other-{self.suffix}", self.payload(occurrence=self.occurrence),
                Policy(**{**self.policy.__dict__, "max_requests": 9}),
            )
        recreated = FirestoreTransactionalCoordinator(second_client, clock=self.clock).get(first.job_id)
        self.assertEqual(recreated.identity["occurrence_id"], self.occurrence)

    def test_concurrent_create_same_occurrence_with_two_keys_creates_one_job(self):
        second_client = create_guarded_emulator_client()
        self.clients.append(second_client)
        second = FirestoreTransactionalCoordinator(second_client, clock=self.clock)
        barrier = threading.Barrier(2)
        def create(adapter, key):
            barrier.wait(timeout=5)
            return adapter.create(key, self.payload(), self.policy)
        alternate_key = f"alternate-{self.suffix}"
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda args: create(*args), [
                (self.jobs, self.idempotency_key), (second, alternate_key),
            ]))
        self.assertEqual(jobs[0].job_id, jobs[1].job_id)
        self.client.collection("_ts3_idempotency").document(_doc_id(alternate_key)).delete()

    def test_expired_deadline_allows_existing_occurrence_replay_but_not_new_create(self):
        original = self.create_job()
        self.clock.advance(3601)
        replay = self.jobs.create(self.idempotency_key, self.payload(), self.policy)
        self.assertEqual(replay.job_id, original.job_id)
        with self.assertRaises(DeadlineExceeded):
            self.jobs.create(f"late-{self.suffix}", self.payload(f"late-occurrence-{self.suffix}"), self.policy)

    def test_two_clients_racing_for_one_job_yield_one_owner(self):
        self.create_job()
        second_client = create_guarded_emulator_client()
        self.clients.append(second_client)
        second = FirestoreTransactionalCoordinator(second_client, clock=self.clock)
        barrier = threading.Barrier(2)
        def claim(adapter, owner):
            barrier.wait(timeout=5)
            try:
                return adapter.claim(self.job_id, owner)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: claim(*pair), [(self.jobs, "a"), (second, "b")]))
        winners = [job for job in results if job is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.jobs.get(self.job_id).owner, winners[0].owner)

    def test_sdk_callback_retry_does_not_double_charge_budget(self):
        self.create_job()
        original = Transaction._commit
        attempts = {"count": 0}
        def abort_first_commit(transaction):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise Aborted("synthetic transaction contention")
            return original(transaction)
        with patch.object(Transaction, "_commit", abort_first_commit):
            claimed = self.jobs.claim(self.job_id, "retry-worker")
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(claimed.refresh_budget.reads, 3)
        self.assertEqual(claimed.refresh_budget.writes, 4)

    def test_two_clients_racing_for_one_resource_yield_monotonic_single_fence(self):
        first = self.create_job()
        second_occurrence = f"resource-racer-{self.suffix}"
        second_key = f"resource-racer-key-{self.suffix}"
        second_job = self.jobs.create(second_key, self.payload(second_occurrence), self.policy)
        first_claim = self.jobs.claim(first.job_id, "resource-a")
        second_claim = self.jobs.claim(second_job.job_id, "resource-b")
        other_client = create_guarded_emulator_client()
        self.clients.append(other_client)
        other_datasets = FirestoreTransactionalDatasetStore(other_client, clock=self.clock)
        barrier = threading.Barrier(2)
        def claim(store, job, owner):
            barrier.wait(timeout=5)
            try:
                return store.claim_resource(self.resource, job.job_id, owner, job.fencing, job.revision)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda args: claim(*args), [
                (self.datasets, first_claim, "resource-a"),
                (other_datasets, second_claim, "resource-b"),
            ]))
        fences = [value for value in outcomes if value is not None]
        self.assertEqual(len(fences), 1)
        self.assertEqual(fences[0], 1)
        self.client.collection("_ts3_idempotency").document(_doc_id(second_key)).delete()
        self.client.collection("_ts3_occurrences").document(_doc_id(second_occurrence)).delete()
        self.client.collection("_ts3_jobs").document(second_job.job_id).delete()

    def test_resource_fence_increases_after_expiry_and_rejects_stale_owner(self):
        first = self.create_job()
        first, first_token = self.claim_job(owner="old")
        self.clock.advance(31)
        second_occurrence = f"takeover-{self.suffix}"
        second_key = f"takeover-key-{self.suffix}"
        second_job = self.jobs.create(second_key, self.payload(second_occurrence), self.policy)
        second_claim = self.jobs.claim(second_job.job_id, "new")
        second_token = self.datasets.claim_resource(self.resource, second_job.job_id, "new",
                                                    second_claim.fencing, second_claim.revision)
        self.assertEqual(second_token, first_token + 1)
        current = self.jobs.get(second_job.job_id)
        with self.assertRaises(LeaseLost):
            self.datasets.heartbeat_resource(self.resource, first.job_id, "old", first.fencing,
                                             first.revision, first_token)
        with self.assertRaises(LeaseLost):
            self.jobs.transition(first.job_id, "old", first.fencing, first.revision,
                                 State.FAILED, reason_code="STALE_OWNER")
        self.assertEqual(current.owner, "new")
        self.client.collection("_ts3_idempotency").document(_doc_id(second_key)).delete()
        self.client.collection("_ts3_occurrences").document(_doc_id(second_occurrence)).delete()
        self.client.collection("_ts3_jobs").document(second_job.job_id).delete()

    def test_resource_claim_lost_ack_recovers_durable_fence(self):
        self.create_job()
        claimed = self.jobs.claim(self.job_id, "worker")
        original = Transaction._commit
        raised = {"value": False}
        def commit_then_drop_ack(transaction):
            result = original(transaction)
            if not raised["value"]:
                raised["value"] = True
                raise ServiceUnavailable("synthetic lost lease acknowledgement")
            return result
        with patch.object(Transaction, "_commit", commit_then_drop_ack):
            with self.assertRaises(TransactionOutcomeUnknown):
                self.datasets.claim_resource(self.resource, self.job_id, "worker",
                                             claimed.fencing, claimed.revision)
        recovered = self.datasets.get_owned_resource_lease(self.resource, self.job_id,
                                                           "worker", claimed.fencing)
        self.assertEqual(recovered["resource_fencing"], 1)
        self.assertEqual(recovered["pointer_revision"], 0)

    def test_atomic_publish_and_receipt_replay(self):
        self.create_job()
        ready, token = self.prepare_ready_dataset(f"dataset-{self.suffix}-f1")
        self.assertEqual(self.datasets.publish(ready.job_id and f"dataset-{self.suffix}-f1", self.resource,
                                               ready.job_id, "worker", ready.fencing, ready.revision,
                                               token, None, 0), "PUBLISHED")
        self.assertEqual(self.datasets.active(self.resource), (f"dataset-{self.suffix}-f1", 1))
        completed = self.jobs.get(ready.job_id)
        self.assertEqual(completed.state, State.COMPLETED)
        self.assertEqual(completed.completion_evidence.manifest_id, f"dataset-{self.suffix}-f1")
        self.assertEqual(completed.completion_evidence.expected_documents, 7)
        self.assertEqual(self.datasets.replay_publication(self.resource, ready.job_id, "worker",
                                                          f"dataset-{self.suffix}", True, None, 0),
                         "ALREADY_PUBLISHED")

    def test_old_receipt_replay_after_newer_dataset_does_not_rewind_pointer(self):
        first_job = self.create_job()
        first_base = f"dataset-old-{self.suffix}"
        first_ready, first_token = self.prepare_ready_dataset(f"{first_base}-f1")
        self.assertEqual(self.datasets.publish(f"{first_base}-f1", self.resource, first_job.job_id,
                                               "worker", first_ready.fencing, first_ready.revision,
                                               first_token, None, 0), "PUBLISHED")
        active_first, revision_first = self.datasets.active(self.resource)
        self.assertEqual((active_first, revision_first), (f"{first_base}-f1", 1))

        self.clock.advance(31)
        next_occurrence = f"next-occurrence-{self.suffix}"
        next_key = f"next-key-{self.suffix}"
        next_job = self.jobs.create(next_key, self.payload(next_occurrence), self.policy)
        next_claim = self.jobs.claim(next_job.job_id, "worker-next")
        next_token = self.datasets.claim_resource(self.resource, next_job.job_id, "worker-next",
                                                 next_claim.fencing, next_claim.revision)
        next_dataset = f"dataset-next-{self.suffix}-f{next_token}"
        next_ready, _ = self.prepare_ready_dataset(next_dataset, owner="worker-next", job_id=next_job.job_id,
                                                   token=next_token)
        self.assertEqual(self.datasets.publish(next_dataset, self.resource, next_job.job_id, "worker-next",
                                               next_ready.fencing, next_ready.revision, next_token,
                                               active_first, revision_first), "PUBLISHED")
        current = self.datasets.active(self.resource)
        self.assertEqual(current, (next_dataset, 2))
        self.assertEqual(self.datasets.replay_publication(self.resource, first_job.job_id, "worker",
                                                          first_base, True, None, 0), "ALREADY_PUBLISHED")
        self.assertEqual(self.datasets.active(self.resource), current)
        self.client.collection("_ts3_idempotency").document(_doc_id(next_key)).delete()
        self.client.collection("_ts3_occurrences").document(_doc_id(next_occurrence)).delete()
        self.client.collection("_ts3_jobs").document(next_job.job_id).delete()

    def test_publish_race_uses_pointer_cas_and_cleanup_reservation(self):
        self.create_job()
        first_id, second_id = f"candidate-a-{self.suffix}", f"candidate-b-{self.suffix}"
        ready, token = self.prepare_ready_dataset(first_id)
        self.prepare_ready_dataset(second_id, owner="worker", token=token)
        current = self.jobs.get(ready.job_id)
        other_client = create_guarded_emulator_client()
        self.clients.append(other_client)
        other_datasets = FirestoreTransactionalDatasetStore(other_client, clock=self.clock)
        barrier = threading.Barrier(2)
        def publish(store, dataset_id):
            barrier.wait(timeout=5)
            try:
                return store.publish(dataset_id, self.resource, ready.job_id, "worker",
                                     current.fencing, current.revision, token, None, 0)
            except Conflict:
                return "CONFLICT"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda args: publish(*args), [
                (self.datasets, first_id), (other_datasets, second_id),
            ]))
        self.assertEqual(outcomes.count("PUBLISHED"), 1)
        self.assertEqual(outcomes.count("CONFLICT"), 1)
        active, revision = self.datasets.active(self.resource)
        self.assertIn(active, (first_id, second_id))
        self.assertEqual(revision, 1)

    def test_cleanup_reservation_and_dataset_tombstone_block_publish(self):
        self.create_job()
        dataset_id = f"dataset-reserved-{self.suffix}"
        ready, token = self.prepare_ready_dataset(dataset_id)
        resource_ref = self.client.collection("_ts3_resources").document(_doc_id(self.resource))
        resource_ref.update({"cleanup_reserved": True})
        with self.assertRaises(Conflict):
            self.datasets.publish(dataset_id, self.resource, ready.job_id, "worker", ready.fencing,
                                  ready.revision, token, None, 0)
        resource_ref.update({"cleanup_reserved": False})
        self.client.collection("_ts3_datasets").document(dataset_id).update({"tombstone": True})
        with self.assertRaises(Conflict):
            self.datasets.publish(dataset_id, self.resource, ready.job_id, "worker", ready.fencing,
                                  ready.revision, token, None, 0)
        self.assertEqual(self.datasets.active(self.resource), (None, 0))

    def test_cancellation_committed_before_publish_prevents_pointer_change(self):
        self.create_job()
        dataset_id = f"dataset-cancel-race-{self.suffix}"
        ready, token = self.prepare_ready_dataset(dataset_id)
        cancelling = self.jobs.request_cancel(ready.job_id, ready.revision)
        self.assertEqual(cancelling.state, State.CANCELLING)
        with self.assertRaises(Conflict):
            self.datasets.publish(dataset_id, self.resource, cancelling.job_id, "worker",
                                  cancelling.fencing, cancelling.revision, token, None, 0)
        self.assertEqual(self.datasets.active(self.resource), (None, 0))

    def test_publish_budget_exhaustion_keeps_job_validating_and_pointer_old(self):
        self.create_job()
        dataset_id = f"dataset-budget-{self.suffix}"
        ready, token = self.prepare_ready_dataset(dataset_id)
        job_ref = self.client.collection("_ts3_jobs").document(ready.job_id)
        record = job_ref.get().to_dict()
        budget = dict(record["refresh_budget"])
        budget["max_reads"] = budget["reads"]
        job_ref.update({"refresh_budget": budget})
        with self.assertRaises(RefreshError) as caught:
            self.datasets.publish(dataset_id, self.resource, ready.job_id, "worker",
                                  ready.fencing, ready.revision, token, None, 0)
        self.assertEqual(caught.exception.code, ErrorCode.QUOTA_EXCEEDED)
        self.assertEqual(self.datasets.active(self.resource), (None, 0))
        self.assertEqual(job_ref.get().to_dict()["state"], State.VALIDATING.value)

    def test_budget_exhaustion_preserves_reserved_stale_abort_capacity(self):
        self.create_job()
        tiny_policy = Policy(
            max_attempts=2, max_requests=1, retry_budget=0,
            deadline=self.clock.now + timedelta(hours=1), lease_seconds=30,
            heartbeat_seconds=10, backoff_seconds=0, estimate=Estimate(), budget=Estimate(),
            operation_budget=OfflineBudget(4, 5),
        )
        key = f"tiny-budget-{self.suffix}"
        occurrence = f"tiny-occurrence-{self.suffix}"
        job = self.jobs.create(key, self.payload(occurrence), tiny_policy)
        claimed = self.jobs.claim(job.job_id, "budget-worker")
        with self.assertRaises(RefreshError) as caught:
            self.jobs.heartbeat(job.job_id, "budget-worker", claimed.fencing, claimed.revision)
        self.assertEqual(caught.exception.code, ErrorCode.QUOTA_EXCEEDED)
        job_ref = self.client.collection("_ts3_jobs").document(job.job_id)
        before = job_ref.get().to_dict()
        self.assertTrue(before["recovery_reserved"])
        self.assertEqual((before["refresh_budget"]["reads"], before["refresh_budget"]["writes"]), (3, 4))
        self.clock.advance(31)
        aborted = self.jobs.abort_stale(job.job_id)
        self.assertEqual(aborted.state, State.ABORTED)
        self.assertFalse(aborted.recovery_reserved)
        self.assertEqual((aborted.refresh_budget.reads, aborted.refresh_budget.writes), (4, 5))
        self.client.collection("_ts3_idempotency").document(_doc_id(key)).delete()
        self.client.collection("_ts3_occurrences").document(_doc_id(occurrence)).delete()

    def test_commit_acknowledgement_loss_is_recoverable_by_receipt(self):
        self.create_job()
        ready, token = self.prepare_ready_dataset(f"dataset-lost-ack-{self.suffix}")
        original = Transaction._commit
        raised = {"value": False}
        def commit_then_drop_ack(transaction):
            result = original(transaction)
            if not raised["value"]:
                raised["value"] = True
                raise ServiceUnavailable("synthetic lost acknowledgement")
            return result
        with patch.object(Transaction, "_commit", commit_then_drop_ack):
            with self.assertRaises(TransactionOutcomeUnknown):
                self.datasets.publish(f"dataset-lost-ack-{self.suffix}", self.resource,
                                      ready.job_id, "worker", ready.fencing, ready.revision,
                                      token, None, 0)
        self.assertEqual(self.datasets.replay_publication(self.resource, ready.job_id, "worker",
                                                          f"dataset-lost-ack-{self.suffix}", False, None, 0),
                         "ALREADY_PUBLISHED")
        self.assertEqual(self.datasets.active(self.resource), (f"dataset-lost-ack-{self.suffix}", 1))


if __name__ == "__main__":
    unittest.main()
