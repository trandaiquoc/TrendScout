"""Safety extensions; synthetic, in-process only."""
from copy import deepcopy
from datetime import timedelta
import unittest
from app.datasets.fake import FakeDatasetStore, digest
from app.datasets.manifest import validate_manifest
from app.jobs.fake import FakeCoordinator
from app.jobs.contracts import Policy, State
from app.storage_analysis.planning import Estimate
from app.workers.safety import OfflineBudget, RefreshError, ErrorCode, RetryPolicy
from app.workers.refresh import RefreshWorker
from test_job_coordinator import Clock, payload


def manifest(job_id):
    fields=dict(occurrence_id='o1',source_id='wikimedia',country='US',access='all-access',project='all',analysis_date='2026-01-08',window_start='2026-01-01',window_end='2026-01-08',source_version='v1',processing_version='v1',cleaning_version='v1',classification_version='v1',algorithm_version='v1',provenance='synthetic://fixture',quality_report_ref='report-v1')
    shards={'full':{'items':[1]},'empty':{'items':[]}}
    m=dict(fields,manifest_version='offline-manifest-v1',dataset_id='dataset',job_id=job_id,resource_id='stream',required_partitions=['full','empty'],shards={k:{'sha256':digest(v),'count':len(v['items'])} for k,v in shards.items()},expected_count=1,quality_report={'policy_version':'offline-quality-v1','status':'PASS','observed_count':1})
    return fields,shards,m


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock(); self.jobs=FakeCoordinator(self.clock); self.repo=FakeDatasetStore(self.jobs)
        self.policy=Policy(3,20,2,self.clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate())
    def test_manifest_contract_and_tamper(self):
        j=self.jobs.claim(self.jobs.create('key',payload(),self.policy).job_id,'a')
        token=self.repo.claim_resource('stream',j.job_id,j.owner,j.fencing,j.revision)
        meta,shards,m=manifest(j.job_id)
        self.assertTrue(validate_manifest(m,dataset_id='dataset',job_id=j.job_id,resource_id='stream',metadata=meta,shards=shards,hash_fn=digest))
        cases=[lambda x:x.pop('source_id'),lambda x:x.update(country='CA'),lambda x:x.update(window_start='2027-01-01'),lambda x:x['shards']['full'].update(count=9),lambda x:x['shards']['full'].update(sha256='bad'),lambda x:x.update(provenance='bad'),lambda x:x.pop('quality_report'),lambda x:x['required_partitions'].append('missing')]
        for mutate in cases:
            bad=deepcopy(m); mutate(bad)
            self.assertFalse(validate_manifest(bad,dataset_id='dataset',job_id=j.job_id,resource_id='stream',metadata=meta,shards=shards,hash_fn=digest))
        for state in (State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING): j=self.jobs.transition(j.job_id,j.owner,j.fencing,j.revision,state)
        self.repo.draft('dataset','stream',j.job_id,j.owner,j.fencing,j.revision,token,meta)
        for key,value in shards.items(): self.repo.put_shard('dataset','stream',j.job_id,j.owner,j.fencing,j.revision,token,key,value)
        self.repo.seal('dataset','stream',j.job_id,j.owner,j.fencing,j.revision,token,m)
        j=self.jobs.transition(j.job_id,j.owner,j.fencing,j.revision,State.VALIDATING)
        self.assertTrue(self.repo.verify('dataset','stream',j.job_id,j.owner,j.fencing,j.revision,token,quality_ok=False))
        self.repo.datasets['dataset']['shards']['full']['items'].append(2)
        with self.assertRaises(Exception): self.repo.publish('dataset','stream',j.job_id,j.owner,j.fencing,j.revision,token,None,0)
        self.assertEqual(self.repo.active('stream'),(None,0))

    def test_budget_stops_at_each_write_boundary(self):
        for limit in (0,1,2,3,4):
            with self.subTest(limit=limit):
                c=FakeCoordinator(self.clock); repo=FakeDatasetStore(c); j=c.create(str(limit),payload(str(limit)),self.policy)
                worker=RefreshWorker(c,repo,lambda:[1],lambda x:x,lambda x:{'metadata':{'source':'s'},'shards':{'s':{'items':x}},'quality_ok':True},budget=OfflineBudget(max_reads=100,max_writes=limit))
                with self.assertRaises(RefreshError) as caught: worker.run(j.job_id,'a','stream','d',expected_active=None,expected_revision=0)
                self.assertEqual(caught.exception.code,ErrorCode.QUOTA_EXCEEDED)
                self.assertEqual(repo.active('stream'),(None,0))
                self.assertEqual(c.get(j.job_id).state,State.FAILED)

    def test_retryable_fetch_then_success_same_job(self):
        job=self.jobs.create('recover',payload('recover'),self.policy)
        calls=[]
        def fetch():
            calls.append(1)
            if len(calls)==1: raise RefreshError(ErrorCode.RETRYABLE_SOURCE_ERROR)
            return [1]
        output=lambda rows:{'metadata':{'source':'synthetic'},'shards':{'s':{'items':rows}},'quality_ok':True}
        worker=RefreshWorker(self.jobs,self.repo,fetch,lambda x:x,output,retry_policy=RetryPolicy(3,2,10))
        with self.assertRaises(RefreshError): worker.run(job.job_id,'a','stream','d',expected_active=None,expected_revision=0,fresh_on_retry=True)
        self.assertEqual(self.jobs.get(job.job_id).state,State.RETRY_WAIT)
        self.clock.advance(3)
        result=worker.run(job.job_id,'b','stream','d',expected_active=None,expected_revision=0,fresh_on_retry=True)
        self.assertEqual(result,'PUBLISHED')
        self.assertEqual((self.jobs.get(job.job_id).attempts,self.repo.active('stream')), (2,('d-f2',1)))

    def test_retry_classification_and_coordinator_backoff(self):
        policy=RetryPolicy(3,2,10); error=RefreshError(ErrorCode.RATE_LIMITED,3)
        self.assertEqual(policy.delay(error,1,self.clock.now,self.policy.deadline),timedelta(seconds=3))
        self.assertIsNone(policy.delay(RefreshError(ErrorCode.INVALID_SOURCE_DATA),1,self.clock.now,self.policy.deadline))
        self.assertIsNone(policy.delay(error,3,self.clock.now,self.policy.deadline))
        j=self.jobs.create('retry',payload('retry'),self.policy)
        worker=RefreshWorker(self.jobs,self.repo,lambda:(_ for _ in ()).throw(error),lambda x:x,lambda x:x,retry_policy=policy)
        with self.assertRaises(RefreshError): worker.run(j.job_id,'a','stream','d',expected_active=None,expected_revision=0)
        self.assertEqual(self.jobs.get(j.job_id).state,State.RETRY_WAIT)
        with self.assertRaises(Exception): self.jobs.claim(j.job_id,'b')
        self.clock.advance(3)
        self.assertEqual(self.jobs.claim(j.job_id,'b').attempts,2)

if __name__=='__main__': unittest.main()

class PersistedFakeRecoveryTests(unittest.TestCase):
    def test_reload_fake_snapshot_from_temporary_file(self):
        import pickle
        import tempfile
        from pathlib import Path
        from app.jobs.fake import FakeTransactionalStore
        class Crash(BaseException): pass
        clock=Clock(); store=FakeTransactionalStore(); c=FakeCoordinator(clock,store); repo=FakeDatasetStore(c)
        policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate())
        job=c.create('restart',payload('restart'),policy)
        def hook(name):
            if name=='before_seal': raise Crash()
        fn=lambda x:{'metadata':{'source':'synthetic'},'shards':{'a':{'items':x}},'quality_ok':True}
        with self.assertRaises(Crash): RefreshWorker(c,repo,lambda:[1],lambda x:x,fn,checkpoint_hook=hook).run(job.job_id,'a','stream','draft',expected_active=None,expected_revision=0,fresh_on_retry=True)
        # Test-only snapshot of trusted fake objects, never load an untrusted pickle.
        state={'jobs':store.jobs,'keys':store.keys,'occurrences':store.occurrences,'resources':repo.resources,'datasets':repo.datasets,'publications':repo.publications}
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'fake-state.bin'; file.write_bytes(pickle.dumps(state))
            del state,repo,c,store
            restored=pickle.loads(file.read_bytes())
            store2=FakeTransactionalStore(); store2.jobs=restored['jobs']; store2.keys=restored['keys']; store2.occurrences=restored['occurrences']
            c2=FakeCoordinator(clock,store2); repo2=FakeDatasetStore(c2)
            repo2.resources=restored['resources']; repo2.datasets=restored['datasets']; repo2.publications=restored['publications']
            clock.advance(31)
            self.assertEqual(RefreshWorker(c2,repo2,lambda:[1],lambda x:x,fn).run(job.job_id,'b','stream','draft',expected_active=None,expected_revision=0,fresh_on_retry=True),'PUBLISHED')
            self.assertEqual(repo2.active('stream'),('draft-f2',1))
            self.assertEqual(repo2.read_dataset('draft-f1')['state'],'DRAFT')

class CrashMatrixTests(unittest.TestCase):
    def test_crash_matrix_restarts_new_draft_or_keeps_published(self):
        class Crash(BaseException): pass
        boundaries=('after_claim','fetch','after_fetch','clean','after_analysis','before_draft','after_draft','after_shard','before_seal','after_seal','before_publish','after_publish')
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
                policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate())
                job=jobs.create(boundary,payload(boundary),policy)
                def hook(name):
                    if name==boundary: raise Crash()
                def fetch():
                    if boundary=='fetch': raise Crash()
                    return [1]
                def clean(rows):
                    if boundary=='clean': raise Crash()
                    return rows
                output=lambda rows:{'metadata':{'source':'synthetic'},'shards':{'a':{'items':rows}},'quality_ok':True}
                with self.assertRaises(Crash): RefreshWorker(jobs,repo,fetch,clean,output,checkpoint_hook=hook).run(job.job_id,'a','stream','draft',expected_active=None,expected_revision=0,fresh_on_retry=True)
                if boundary=='after_publish':
                    self.assertEqual(repo.active('stream'),('draft-f1',1))
                    self.assertEqual(jobs.get(job.job_id).state,State.COMPLETED)
                else:
                    self.assertEqual(repo.active('stream'),(None,0))
                    self.assertNotEqual(jobs.get(job.job_id).state,State.COMPLETED)
                    clock.advance(31)
                    result=RefreshWorker(jobs,repo,lambda:[1],lambda x:x,output).run(job.job_id,'b','stream','draft',expected_active=None,expected_revision=0,fresh_on_retry=True)
                    self.assertEqual(result,'PUBLISHED')
                    self.assertEqual(repo.active('stream')[1],1)
