"""Regression: unverified legacy manifests cannot become active."""
from refresh_fixtures import fixture_policy as Policy
import unittest
from datetime import timedelta
from app.datasets.fake import FakeDatasetStore, digest
from app.jobs.contracts import State, Conflict
from app.jobs.fake import FakeCoordinator
from app.storage_analysis.planning import Estimate
from test_job_coordinator import Clock, payload

class LegacyBoundaryTests(unittest.TestCase):
    def test_legacy_digest_cannot_be_sealed(self):
        clock=Clock(); jobs=FakeCoordinator(clock); store=FakeDatasetStore(jobs)
        policy=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate())
        job=jobs.claim(jobs.create('key',payload(),policy).job_id,'worker')
        token=store.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
        store.draft('d','stream',job.job_id,job.owner,job.fencing,job.revision,token,{'source':'synthetic'})
        with self.assertRaises((ValueError,Conflict)):
            store.seal('d','stream',job.job_id,job.owner,job.fencing,job.revision,token,{'s':digest({'items':[1]})})
        self.assertEqual(store.active('stream'),(None,0))

    def test_forced_legacy_state_cannot_verify_or_publish(self):
        clock=Clock(); jobs=FakeCoordinator(clock); store=FakeDatasetStore(jobs)
        policy=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate())
        job=jobs.claim(jobs.create('key',payload(),policy).job_id,'worker')
        token=store.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
        store.draft('d','stream',job.job_id,job.owner,job.fencing,job.revision,token,{'source':'synthetic'})
        d=store.datasets['d']; d['manifest']={'s':digest({'items':[1]})}; d['state']='VALIDATING'
        self.assertFalse(store.verify('d','stream',job.job_id,job.owner,job.fencing,job.revision,token,quality_ok=True))
        d['state']='READY'; d['verified_counts']={'shards':0,'items':0}
        d['verified']=digest({'manifest':d['manifest'],'metadata':d['metadata'],'counts':d['verified_counts'],'quality_report':None})
        jobs._jobs[job.job_id].state=State.VALIDATING
        with self.assertRaises(Conflict): store.publish('d','stream',job.job_id,job.owner,job.fencing,job.revision,token,None,0)
        self.assertEqual(store.active('stream'),(None,0))

    def test_missing_manifest_inputs_cannot_publish(self):
        from app.workers.refresh import RefreshWorker
        from app.workers.safety import OfflineBudget
        clock=Clock(); jobs=FakeCoordinator(clock); store=FakeDatasetStore(jobs)
        policy=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate())
        job=jobs.create('key',payload(),policy)
        worker=RefreshWorker(jobs,store,lambda:[1],lambda x:x,lambda x:{'metadata':{'source':'s'},'shards':{'s':{'items':x}},'quality_ok':True},budget=OfflineBudget(100,100))
        with self.assertRaises(ValueError): worker.run(job.job_id,'worker','stream','d',expected_active=None,expected_revision=0)
        self.assertEqual(store.active('stream'),(None,0))

    def test_missing_or_inconsistent_persisted_quality_rejects(self):
        from test_refresh_safety import manifest
        for failure in ('missing','count'):
            with self.subTest(failure=failure):
                clock=Clock(); jobs=FakeCoordinator(clock); store=FakeDatasetStore(jobs)
                policy=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate())
                j=jobs.claim(jobs.create(failure,payload(),policy).job_id,'w'); token=store.claim_resource('stream',j.job_id,j.owner,j.fencing,j.revision)
                meta,shards,m=manifest(j.job_id)
                store.draft('dataset','stream',j.job_id,j.owner,j.fencing,j.revision,token,meta)
                for k,v in shards.items(): store.put_shard('dataset','stream',j.job_id,j.owner,j.fencing,j.revision,token,k,v)
                if failure=='count':
                    report=dict(m['quality_report']); report['observed_count']=99
                    store.put_quality_report('dataset','stream',j.job_id,j.owner,j.fencing,j.revision,token,report)
                store.seal('dataset','stream',j.job_id,j.owner,j.fencing,j.revision,token,m)
                self.assertFalse(store.verify('dataset','stream',j.job_id,j.owner,j.fencing,j.revision,token,quality_ok=True))
                self.assertEqual(store.active('stream'),(None,0))
