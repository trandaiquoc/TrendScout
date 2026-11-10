"""Worker replay after committed publish must never advance or rewind the pointer."""
import unittest
from datetime import timedelta
from app.jobs.fake import FakeCoordinator
from app.jobs.contracts import Policy
from app.datasets.fake import FakeDatasetStore
from app.storage_analysis.planning import Estimate
from app.workers.safety import OfflineBudget
from app.workers.refresh import RefreshWorker as _RefreshWorker
from refresh_fixtures import strict_worker

def RefreshWorker(*args, **kwargs): return strict_worker(_RefreshWorker,*args,**kwargs)
from refresh_fixtures import strict_output
from test_job_coordinator import Clock, payload


class WorkerReplayTests(unittest.TestCase):
    def test_lost_ack_replay_after_newer_publication(self):
        class Crash(BaseException): pass
        clock=Clock(); jobs=FakeCoordinator(clock); store=FakeDatasetStore(jobs)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
        a=jobs.create('a',payload('a'),p)
        def hook(point):
            if point=='after_publish': raise Crash()
        def worker(hook=None): return RefreshWorker(jobs,store,lambda:[1],lambda x:x,lambda x:strict_output({'shards':{'s':{'items':x}},'quality_ok':True}),budget=OfflineBudget(10000,10000),checkpoint_hook=hook)
        with self.assertRaises(Crash): worker(hook).run(a.job_id,'owner-a','stream','dataset-a',expected_active=None,expected_revision=0,fresh_on_retry=True)
        self.assertEqual(store.active('stream'),('dataset-a-f1',1))
        clock.advance(31)
        b=jobs.create('b',payload('b'),p)
        self.assertEqual(worker().run(b.job_id,'owner-b','stream','dataset-b',expected_active='dataset-a-f1',expected_revision=1,fresh_on_retry=True),'PUBLISHED')
        self.assertEqual(worker().run(a.job_id,'owner-a','stream','dataset-a',expected_active=None,expected_revision=0,fresh_on_retry=True),'ALREADY_PUBLISHED')
        self.assertEqual(store.active('stream'),('dataset-b-f2',2))
        from app.jobs.contracts import Conflict
        with self.assertRaises(Conflict): worker().run(a.job_id,'forged-owner','stream','dataset-a',expected_active=None,expected_revision=0,fresh_on_retry=True)
        with self.assertRaises(Conflict): worker().run(a.job_id,'owner-a','stream','dataset-a',expected_active='forged',expected_revision=0,fresh_on_retry=True)
        self.assertEqual(store.active('stream'),('dataset-b-f2',2))
        self.assertEqual(len(store.publications),2)

    def test_reload_receipt_budget_idempotency_and_fence(self):
        import pickle,tempfile
        from pathlib import Path
        from app.jobs.fake import FakeTransactionalStore
        clock=Clock(); backing=FakeTransactionalStore(); jobs=FakeCoordinator(clock,backing); store=FakeDatasetStore(jobs)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
        a=jobs.create('a',payload('a'),p)
        def output(rows): return strict_output({'shards':{'s':{'items':rows}},'quality_ok':True})
        w=RefreshWorker(jobs,store,lambda:[1],lambda x:x,output,budget=OfflineBudget(10000,10000))
        self.assertEqual(w.run(a.job_id,'owner-a','stream','dataset-a',expected_active=None,expected_revision=0,fresh_on_retry=True),'PUBLISHED')
        import copy
        used=copy.deepcopy(jobs._jobs[a.job_id].refresh_budget)
        data={'jobs':backing.jobs,'keys':backing.keys,'occurrences':backing.occurrences,'resources':store.resources,'datasets':store.datasets,'publications':store.publications}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'state'; path.write_bytes(pickle.dumps(data)); del data,store,jobs,backing
            state=pickle.loads(path.read_bytes()); backing2=FakeTransactionalStore()
            backing2.jobs=state['jobs']; backing2.keys=state['keys']; backing2.occurrences=state['occurrences']
            backing2.resources=state['resources']; backing2.datasets=state['datasets']; backing2.publications=state['publications']
            jobs=FakeCoordinator(clock,backing2); store=FakeDatasetStore(jobs)
            restored_budget=jobs._jobs[a.job_id].refresh_budget
            self.assertEqual(restored_budget,used)
            self.assertGreater(restored_budget.metadata_operations,0)
            self.assertGreater(restored_budget.heartbeats,0)
            self.assertGreater(restored_budget.source_requests,0)
            self.assertEqual(jobs.create('a',payload('a'),p).job_id,a.job_id)
            from app.jobs.contracts import Conflict
            with self.assertRaises(Conflict): jobs.create('different-key',dict(payload('a'),config_version='v2'),p)
            self.assertEqual(len(jobs._jobs),1)
            w=RefreshWorker(jobs,store,lambda:[1],lambda x:x,output,budget=OfflineBudget(10000,10000))
            self.assertEqual(w.run(a.job_id,'owner-a','stream','dataset-a',expected_active=None,expected_revision=0,fresh_on_retry=True),'ALREADY_PUBLISHED')
            clock.advance(31); b=jobs.create('b',payload('b'),p)
            self.assertEqual(w.run(b.job_id,'owner-b','stream','dataset-b',expected_active='dataset-a-f1',expected_revision=1,fresh_on_retry=True),'PUBLISHED')
            self.assertEqual(store.active('stream'),('dataset-b-f2',2))

    def test_publication_failure_during_job_record_is_atomic(self):
        from unittest.mock import patch
        from test_refresh_offline import PublicationTests
        fixture=PublicationTests(); fixture.setUp()
        job=fixture.job('atomic'); token=fixture.repo.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
        job,ok=fixture.ready(job,token,'dataset'); self.assertTrue(ok)
        with patch.object(fixture.c,'_record',side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError): fixture.repo.publish('dataset','stream',job.job_id,job.owner,job.fencing,job.revision,token,None,0)
        self.assertEqual(fixture.repo.active('stream'),(None,0))
        self.assertEqual(fixture.repo.read_dataset('dataset')['state'],'READY')
        self.assertEqual(fixture.c.get(job.job_id).state.value,'VALIDATING')
        self.assertEqual(fixture.repo.publications,{})

    def test_retry_after_partial_without_opt_in_uses_new_namespace(self):
        class Crash(BaseException): pass
        clock=Clock(); jobs=FakeCoordinator(clock); store=FakeDatasetStore(jobs)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
        job=jobs.create('retry-default',payload('retry-default'),p)
        def hook(point):
            if point=='before_seal': raise Crash()
        def worker(hook=None): return RefreshWorker(jobs,store,lambda:[1],lambda x:x,lambda x:strict_output({'shards':{'s':{'items':x}},'quality_ok':True}),budget=OfflineBudget(10000,10000),checkpoint_hook=hook)
        with self.assertRaises(Crash): worker(hook).run(job.job_id,'a','stream','draft',expected_active=None,expected_revision=0)
        self.assertEqual(store.read_dataset('draft')['state'],'DRAFT')
        clock.advance(31)
        self.assertEqual(worker().run(job.job_id,'b','stream','draft',expected_active=None,expected_revision=0),'PUBLISHED')
        self.assertEqual(store.active('stream'),('draft-f2',1))
        self.assertEqual(worker().run(job.job_id,'b','stream','draft',expected_active=None,expected_revision=0),'ALREADY_PUBLISHED')

    def test_worker_publish_cas_conflict_preserves_active(self):
        from app.jobs.contracts import Conflict
        clock=Clock(); jobs=FakeCoordinator(clock); store=FakeDatasetStore(jobs)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
        def worker(): return RefreshWorker(jobs,store,lambda:[1],lambda x:x,lambda x:strict_output({'shards':{'s':{'items':x}},'quality_ok':True}),budget=OfflineBudget(10000,10000))
        first=jobs.create('cas-first',payload('cas-first'),p)
        self.assertEqual(worker().run(first.job_id,'a','stream','first',expected_active=None,expected_revision=0),'PUBLISHED')
        clock.advance(31)
        second=jobs.create('cas-second',payload('cas-second'),p)
        with self.assertRaises(Conflict): worker().run(second.job_id,'b','stream','second',expected_active=None,expected_revision=0)
        self.assertEqual(store.active('stream'),('first',1))
        self.assertNotEqual(jobs._jobs[second.job_id].state.value,'COMPLETED')
        self.assertEqual(len(store.publications),1)
