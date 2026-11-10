"""Fake-only coordination tests; no claim of distributed Firestore safety."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
import unittest

from app.jobs.contracts import (State, Policy, CompletionEvidence, Conflict, LeaseLost,
    InvalidTransition, BudgetExceeded, DeadlineExceeded, legacy_state)
from app.jobs.fake import FakeCoordinator, FakeTransactionalStore
from app.storage_analysis.planning import Estimate


class Clock:
    def __init__(self): self.now=datetime(2026,1,1,tzinfo=timezone.utc)
    def __call__(self): return self.now
    def advance(self, seconds): self.now += timedelta(seconds=seconds)


def payload(occurrence='once'):
    return dict(job_type='analysis',source='wikimedia',scope={'country':'US'},window=7,
                config_version='v1',occurrence_id=occurrence,tenant_scope='public')


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock(); self.coordinator=FakeCoordinator(self.clock)
        self.policy=Policy(3,10,2,self.clock.now+timedelta(hours=1),30,10,5,Estimate(writes=4),Estimate(writes=10))
        self.job=self.coordinator.create('key',payload(),self.policy)

    def change(self,job,target,**kw):
        return self.coordinator.transition(job.job_id,job.owner,job.fencing,job.revision,target,**kw)

    def test_identity_idempotency_and_conflict(self):
        self.assertEqual(self.coordinator.create('key',payload(),self.policy).job_id,self.job.job_id)
        self.assertEqual(self.coordinator.create('alternate-key',payload(),self.policy).job_id,self.job.job_id)
        for other in (payload('another'),dict(payload(),window=14),dict(payload(),config_version='v2')):
            with self.assertRaises(Conflict): self.coordinator.create('key',other,self.policy)
        self.assertNotEqual(self.coordinator.create('other',payload('another'),self.policy).job_id,self.job.job_id)
        self.clock.advance(4000)
        self.assertEqual(self.coordinator.create('key',payload(),self.policy).job_id,self.job.job_id)

    def test_concurrent_claim_fence_and_stale_owner(self):
        barrier=Barrier(2)
        def try_claim(owner):
            barrier.wait()
            try: return self.coordinator.claim(self.job.job_id,owner)
            except Conflict: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs=list(pool.map(try_claim,['A','B']))
        winner=next(j for j in jobs if j)
        self.assertEqual(sum(x is not None for x in jobs),1)
        self.clock.advance(31)
        newer=self.coordinator.claim(self.job.job_id,'C')
        self.assertEqual(newer.fencing,winner.fencing+1)
        for fn in (lambda:self.coordinator.heartbeat(winner.job_id,winner.owner,winner.fencing,winner.revision),
                   lambda:self.change(winner,State.FETCHING),
                   lambda:self.coordinator.release(winner.job_id,winner.owner,winner.fencing,winner.revision),
                   lambda:self.coordinator.check_publish(winner.job_id,winner.owner,winner.fencing,winner.revision)):
            with self.assertRaises(LeaseLost): fn()
        self.assertEqual(self.coordinator.get(newer.job_id).owner,'C')

    def test_state_completion_and_revision(self):
        j=self.coordinator.claim(self.job.job_id,'A')
        with self.assertRaises(InvalidTransition): self.change(j,State.COMPLETED)
        with self.assertRaises(InvalidTransition): self.change(j,'INVALID')
        old=j
        for state in (State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING,State.VALIDATING): j=self.change(j,state)
        with self.assertRaises(Conflict): self.change(old,State.FETCHING)
        self.assertTrue(self.coordinator.check_publish(j.job_id,j.owner,j.fencing,j.revision))
        with self.assertRaises(InvalidTransition): self.change(j,State.COMPLETED,evidence=CompletionEvidence(5,4,True,'manifest'))
        j=self.change(j,State.COMPLETED,evidence=CompletionEvidence(5,5,True,'manifest'))
        self.assertEqual(j.completion_evidence.persisted_documents,5)
        with self.assertRaises(InvalidTransition): self.change(j,State.FETCHING)
        self.assertIsNone(self.coordinator.release(j.job_id,j.owner,j.fencing,j.revision).owner)

    def test_cancel_checkpoint_and_heartbeat(self):
        j=self.coordinator.claim(self.job.job_id,'A')
        self.clock.advance(5)
        j=self.coordinator.heartbeat(j.job_id,j.owner,j.fencing,j.revision)
        j=self.coordinator.checkpoint(j.job_id,j.owner,j.fencing,j.revision,requests=3)
        cancel=self.coordinator.request_cancel(j.job_id,j.revision)
        with self.assertRaises(Conflict): self.coordinator.checkpoint(j.job_id,j.owner,j.fencing,j.revision)
        self.assertEqual(self.coordinator.checkpoint(cancel.job_id,cancel.owner,cancel.fencing,cancel.revision).state,State.CANCELLING)
        cancelled=self.change(cancel,State.CANCELLED)
        self.assertEqual(cancelled.state,State.CANCELLED)
        self.assertGreater(len(cancelled.audit),3)

    def test_expired_cancel_and_budget(self):
        j=self.coordinator.claim(self.job.job_id,'A')
        with self.assertRaises(BudgetExceeded): self.coordinator.checkpoint(j.job_id,j.owner,j.fencing,j.revision,requests=11)
        self.clock.advance(31)
        self.assertEqual(self.coordinator.request_cancel(j.job_id,j.revision).state,State.CANCELLED)
        with self.assertRaises(BudgetExceeded): self.coordinator.create('other',payload('different'),replace(self.policy,budget=Estimate()))

    def test_retry_backoff_attempts_and_replay(self):
        j=self.coordinator.claim(self.job.job_id,'A')
        j=self.change(j,State.FETCHING)
        j=self.change(j,State.RETRY_WAIT,reason_code='NETWORK_ERROR')
        with self.assertRaises(Conflict): self.coordinator.claim(j.job_id,'B')
        self.clock.advance(5); j=self.coordinator.claim(j.job_id,'B')
        j=self.change(j,State.RETRY_WAIT,reason_code='NETWORK_ERROR')
        self.clock.advance(5); j=self.coordinator.claim(j.job_id,'C')
        with self.assertRaises(BudgetExceeded): self.change(j,State.RETRY_WAIT,reason_code='NETWORK_ERROR')
        self.assertEqual(self.coordinator.create('key',payload(),self.policy).job_id,j.job_id)

    def test_crash_in_each_stage_takeover_without_completed(self):
        for stage in (State.QUEUED,State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING,State.VALIDATING):
            with self.subTest(stage=stage):
                c=FakeCoordinator(self.clock); job=c.create(stage.value,payload(stage.value),self.policy); j=c.claim(job.job_id,'old')
                for s in (State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING,State.VALIDATING):
                    if j.state==stage: break
                    j=c.transition(j.job_id,j.owner,j.fencing,j.revision,s)
                self.clock.advance(31); fresh=c.claim(j.job_id,'new')
                self.assertEqual((fresh.state,fresh.fencing),(State.QUEUED,2))
                with self.assertRaises(LeaseLost): c.transition(j.job_id,j.owner,j.fencing,j.revision,State.FAILED,reason_code='ERROR')

    def test_failure_states_and_error_redaction(self):
        for state in (State.FAILED,State.PARTIAL,State.ABORTED):
            c=FakeCoordinator(self.clock); job=c.create(state.value,payload(state.value),self.policy); j=c.claim(job.job_id,'A')
            with self.assertRaises(ValueError): c.transition(j.job_id,j.owner,j.fencing,j.revision,state,reason_code='bad secret text')
            final=c.transition(j.job_id,j.owner,j.fencing,j.revision,state,reason_code='SAFE_ERROR')
            with self.assertRaises(InvalidTransition): c.claim(final.job_id,'B')

    def test_client_restart_with_shared_fake_store(self):
        store=FakeTransactionalStore()
        first=FakeCoordinator(self.clock,store); job=first.create('restart',payload('restart'),self.policy)
        claimed=first.claim(job.job_id,'old')
        second=FakeCoordinator(self.clock,store)
        self.assertEqual(second.create('restart',payload('restart'),self.policy).job_id,job.job_id)
        self.clock.advance(31); replacement=second.claim(job.job_id,'new')
        self.assertEqual(replacement.fencing,claimed.fencing+1)
        with self.assertRaises(LeaseLost): first.heartbeat(job.job_id,'old',claimed.fencing,claimed.revision)

    def test_legacy_mapping_has_no_fabricated_status(self):
        for state in (State.QUEUED,State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING,State.COMPLETED,State.PARTIAL,State.FAILED,State.ABORTED):
            self.assertEqual(legacy_state(state),state.value)
        with self.assertRaises(ValueError): legacy_state(State.CANCELLING)

    def test_deadline_and_invalid_clock(self):
        j=self.coordinator.claim(self.job.job_id,'A'); self.clock.advance(3601)
        with self.assertRaises(LeaseLost): self.coordinator.heartbeat(j.job_id,j.owner,j.fencing,j.revision)
        with self.assertRaises(DeadlineExceeded): self.coordinator.claim(j.job_id,'B')
        with self.assertRaises(ValueError): FakeCoordinator(lambda:datetime.now()).create('key',payload(),self.policy)


if __name__ == '__main__': unittest.main()
