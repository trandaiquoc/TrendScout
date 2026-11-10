"""Regression tests for fail-closed creation of offline job budgets."""
import unittest
from dataclasses import replace
from datetime import timedelta
from app.jobs.contracts import Policy
from app.jobs.fake import FakeCoordinator
from app.storage_analysis.planning import Estimate
from app.workers.safety import OfflineBudget
from test_job_coordinator import Clock, payload


class CreationBudgetTests(unittest.TestCase):
    def test_exhausted_job_can_be_aborted_after_lease_without_new_budget(self):
        from app.datasets.fake import FakeDatasetStore
        from app.jobs.contracts import Conflict, InvalidTransition, LeaseLost, State
        from app.workers.safety import RefreshError
        from app.workers.refresh import RefreshWorker
        from refresh_fixtures import strict_worker, strict_output
        clock=Clock(); c=FakeCoordinator(clock); repo=FakeDatasetStore(c)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(1000,40))
        job=c.create('recover-budget',payload('recover-budget'),p)
        worker=strict_worker(RefreshWorker,c,repo,lambda:[1],lambda x:x,
            lambda x:strict_output({'shards':{'s':{'items':x}},'quality_ok':True}),budget=OfflineBudget(1000,40))
        with self.assertRaises(RefreshError): worker.run(job.job_id,'owner','stream','draft',expected_active=None,expected_revision=0)
        before=c._jobs[job.job_id]; self.assertNotIn(before.state,(State.COMPLETED,State.ABORTED))
        self.assertEqual(repo.read_dataset('draft')['state'],'DRAFT')
        used=(before.refresh_budget.reads,before.refresh_budget.writes)
        with self.assertRaises(Conflict): c.abort_stale(job.job_id)
        clock.advance(31)
        recovered=c.abort_stale(job.job_id)
        self.assertEqual((recovered.state,recovered.reason_code),(State.ABORTED,'STALE_LEASE_RECOVERY'))
        self.assertEqual((recovered.refresh_budget.reads,recovered.refresh_budget.writes),used)
        self.assertLessEqual(recovered.refresh_budget.writes,p.operation_budget.max_writes)
        self.assertEqual(repo.active('stream'),(None,0))
        self.assertEqual(repo.read_dataset('draft')['state'],'DRAFT')
        with self.assertRaises(LeaseLost): c.heartbeat(job.job_id,'owner',before.fencing,before.revision)
        with self.assertRaises(InvalidTransition): c.abort_stale(job.job_id)

    def test_retry_wait_write_exhaustion_recovers_after_fake_reload(self):
        import pickle
        import httpx
        from datetime import date
        from app.datasets.fake import FakeDatasetStore
        from app.jobs.contracts import State
        from app.jobs.fake import FakeTransactionalStore
        from app.workers.offline_pipeline import OfflineWikimediaPipeline
        from app.workers.refresh import RefreshWorker
        from app.workers.safety import RefreshError, RetryPolicy
        clock=Clock(); shared=FakeTransactionalStore(); c=FakeCoordinator(clock,shared); repo=FakeDatasetStore(c)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(1000,12))
        job=c.create('retry-budget',payload('retry-budget'),p)
        source=OfflineWikimediaPipeline(httpx.MockTransport(lambda req:httpx.Response(429,headers={'Retry-After':'4'})),date(2026,1,8),[date(2026,1,8)],clock=clock)
        worker=RefreshWorker(c,repo,source.fetch,source.clean,source.analyze,budget=OfflineBudget(1000,12),retry_policy=RetryPolicy(3,1,10))
        with self.assertRaises(RefreshError): worker.run(job.job_id,'owner','stream','draft',expected_active=None,expected_revision=0)
        record=c._jobs[job.job_id]
        self.assertEqual((record.state,record.requests_used,record.refresh_budget.writes),(State.FETCHING,1,12))
        snapshot=pickle.dumps({'jobs':shared.jobs,'keys':shared.keys,'occurrences':shared.occurrences,'resources':shared.resources})
        data=pickle.loads(snapshot); restored=FakeTransactionalStore()
        restored.jobs=data['jobs']; restored.keys=data['keys']; restored.occurrences=data['occurrences']; restored.resources=data['resources']
        c=FakeCoordinator(clock,restored); clock.advance(31)
        recovered=c.abort_stale(job.job_id)
        self.assertEqual((recovered.state,recovered.reason_code,recovered.refresh_budget.writes),(State.ABORTED,'STALE_LEASE_RECOVERY',12))
        self.assertEqual(FakeDatasetStore(c).active('stream'),(None,0))

    def test_changed_payload_cannot_create_second_budget_for_same_occurrence(self):
        from app.jobs.contracts import Conflict
        clock=Clock(); c=FakeCoordinator(clock)
        p=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(100,100))
        original=payload('same-occurrence'); job=c.create('first-key',original,p)
        variants=(dict(original,config_version='v2'),dict(original,resource_id='other'),
                  dict(original,tenant_scope='other'),dict(original,job_type='other'))
        for index,variant in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(Conflict):
                c.create(f'new-key-{index}',variant,p)
            self.assertEqual(len(c._jobs),1)
        self.assertEqual(c.create('alias',original,p).job_id,job.job_id)

    def test_creation_requires_and_accounts_budget(self):
        clock=Clock(); c=FakeCoordinator(clock)
        p=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate())
        with self.assertRaises((TypeError,ValueError)): c.create('missing',payload(),p)
        p=replace(p,operation_budget=OfflineBudget(10,10))
        job=c.create('one',payload('one'),p)
        self.assertGreater(job.refresh_budget.reads,0)
        self.assertGreater(job.refresh_budget.writes,0)
        again=c.create('one',payload('one'),p)
        self.assertEqual(job.job_id,again.job_id)
        self.assertGreater(again.refresh_budget.reads,job.refresh_budget.reads)

    def test_zero_creation_budget_has_no_job(self):
        clock=Clock(); c=FakeCoordinator(clock)
        for reads,writes in ((0,0),(1,1),(1,2),(2,1)):
            p=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(reads,writes))
            with self.subTest(reads=reads,writes=writes), self.assertRaises(ValueError): c.create('zero',payload('zero'),p)
        self.assertEqual(c._jobs,{})

    def test_no_completion_by_caller_supplied_evidence(self):
        from app.jobs.contracts import State, CompletionEvidence, InvalidTransition
        clock=Clock(); c=FakeCoordinator(clock)
        p=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(100,100))
        job=c.claim(c.create('manual',payload('manual'),p).job_id,'w')
        for state in (State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING,State.VALIDATING):
            job=c.transition(job.job_id,'w',job.fencing,job.revision,state)
        with self.assertRaises(InvalidTransition):
            c.transition(job.job_id,'w',job.fencing,job.revision,State.COMPLETED,evidence=CompletionEvidence(1,1,True,'fake'))

    def test_direct_claim_cannot_bypass_exhausted_job_budget(self):
        from app.workers.safety import RefreshError
        clock=Clock(); c=FakeCoordinator(clock)
        p=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(2,2))
        job=c.create('limited',payload('limited'),p)
        with self.assertRaises(RefreshError): c.claim(job.job_id,'worker')
        self.assertIsNone(c._jobs[job.job_id].owner)

    def test_direct_draft_cannot_bypass_exhausted_budget(self):
        from app.datasets.fake import FakeDatasetStore
        from app.workers.safety import RefreshError
        clock=Clock(); c=FakeCoordinator(clock); repo=FakeDatasetStore(c)
        p=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(20,3))
        job=c.claim(c.create('storage',payload('storage'),p).job_id,'worker')
        with self.assertRaises(RefreshError):
            repo.claim_resource('stream',job.job_id,'worker',job.fencing,job.revision)
        self.assertEqual(repo.active('stream'),(None,0))

    def test_job_lookup_obeys_job_budget(self):
        from app.workers.safety import RefreshError
        clock=Clock(); c=FakeCoordinator(clock)
        p=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(2,100))
        job=c.create('read-limited',payload('read-limited'),p)
        with self.assertRaises(RefreshError): c.get(job.job_id)

    def test_stale_worker_cannot_charge_new_owner_budget(self):
        from app.jobs.contracts import LeaseLost
        clock=Clock(); c=FakeCoordinator(clock)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(100,100))
        a=c.claim(c.create('stale',payload('stale'),p).job_id,'a')
        clock.advance(31); b=c.claim(a.job_id,'b')
        before=(c._jobs[a.job_id].refresh_budget.reads,c._jobs[a.job_id].refresh_budget.writes)
        with self.assertRaises(LeaseLost): c.heartbeat(a.job_id,'a',a.fencing,a.revision)
        self.assertEqual((c._jobs[a.job_id].refresh_budget.reads,c._jobs[a.job_id].refresh_budget.writes),before)
        self.assertEqual(c._jobs[a.job_id].owner,b.owner)

    def test_stale_worker_checkpoint_does_not_debit_replacement(self):
        from app.datasets.fake import FakeDatasetStore
        from app.workers.refresh import RefreshWorker
        from app.jobs.contracts import LeaseLost
        clock=Clock(); c=FakeCoordinator(clock); repo=FakeDatasetStore(c)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(1000,1000))
        job=c.create('race',payload('race'),p); after_takeover=[]
        def fetch():
            clock.advance(31); c.claim(job.job_id,'replacement')
            b=c._jobs[job.job_id].refresh_budget; after_takeover.append((b.reads,b.writes))
            return [1]
        from refresh_fixtures import strict_worker
        worker=strict_worker(RefreshWorker,c,repo,fetch,lambda x:x,lambda x:{},budget=OfflineBudget(1000,1000))
        with self.assertRaises(LeaseLost): worker.run(job.job_id,'old','stream','data',expected_active=None,expected_revision=0)
        b=c._jobs[job.job_id].refresh_budget
        self.assertEqual((b.reads,b.writes),after_takeover[0])

    def test_stale_dataset_operations_do_not_mutate_or_spend(self):
        from app.datasets.fake import FakeDatasetStore
        from app.jobs.contracts import LeaseLost
        clock=Clock(); c=FakeCoordinator(clock); repo=FakeDatasetStore(c)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(100,100))
        old=c.claim(c.create('fenced',payload('fenced'),p).job_id,'old')
        token=repo.claim_resource('stream',old.job_id,'old',old.fencing,old.revision)
        repo.draft('old-draft','stream',old.job_id,'old',old.fencing,old.revision,token,{})
        clock.advance(31); new=c.claim(old.job_id,'new')
        repo.claim_resource('stream',new.job_id,'new',new.fencing,new.revision)
        budget=c._jobs[old.job_id].refresh_budget; before=(budget.reads,budget.writes)
        operations=(lambda:repo.put_shard('old-draft','stream',old.job_id,'old',old.fencing,old.revision,token,'x',{'items':[]}),
                    lambda:repo.put_quality_report('old-draft','stream',old.job_id,'old',old.fencing,old.revision,token,{}),
                    lambda:repo.seal('old-draft','stream',old.job_id,'old',old.fencing,old.revision,token,{}),
                    lambda:repo.verify('old-draft','stream',old.job_id,'old',old.fencing,old.revision,token,quality_ok=True),
                    lambda:repo.heartbeat_resource('stream',old.job_id,'old',old.fencing,old.revision,token),
                    lambda:repo.retry_wait('stream',old.job_id,'old',old.fencing,old.revision,token,'TEMPORARY'))
        for operation in operations:
            with self.assertRaises(LeaseLost): operation()
            self.assertEqual((budget.reads,budget.writes),before)

    def test_unguarded_fetch_cannot_hide_multiple_source_requests(self):
        import httpx
        from app.datasets.fake import FakeDatasetStore
        from app.workers.refresh import RefreshWorker
        from refresh_fixtures import strict_output
        clock=Clock(); c=FakeCoordinator(clock); store=FakeDatasetStore(c); calls=[]
        policy=Policy(2,1,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(1000,1000))
        job=c.create('unguarded',payload('unguarded'),policy)
        def handler(request): calls.append(request); return httpx.Response(200)
        def fetch():
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                client.get('https://mock.invalid/1'); client.get('https://mock.invalid/2')
            return [1]
        worker=RefreshWorker(c,store,fetch,lambda x:x,lambda x:strict_output({'shards':{'x':{'items':x}},'quality_ok':True}),budget=OfflineBudget(1000,1000))
        with self.assertRaises((ValueError,TypeError)):
            worker.run(job.job_id,'owner','stream','dataset',expected_active=None,expected_revision=0)
        self.assertLessEqual(len(calls),1)
        self.assertEqual(store.active('stream'),(None,0))

    def test_replacing_mock_transport_after_construction_is_rejected(self):
        import httpx
        from datetime import date
        from app.workers.offline_pipeline import OfflineWikimediaPipeline
        pipeline=OfflineWikimediaPipeline(httpx.MockTransport(lambda req:httpx.Response(404)),date(2026,1,8),[date(2026,1,8)])
        class ForbiddenTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request): raise AssertionError('transport must never be called')
        pipeline.transport=ForbiddenTransport()
        with self.assertRaises(ValueError): pipeline.fetch()
