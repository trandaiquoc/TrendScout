"""Offline-only scheduler and fenced publication tests."""
from refresh_fixtures import fixture_policy as Policy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
import unittest

from app.jobs.contracts import State, Conflict, LeaseLost
from app.jobs.fake import FakeCoordinator
from app.datasets.fake import FakeDatasetStore, digest
from app.datasets.manifest import build_offline_manifest
from refresh_fixtures import strict_output
from app.scheduling.recurrence import Schedule, FakeScheduler
from app.workers.refresh import RefreshWorker as _RefreshWorker
from app.workers.safety import OfflineBudget

def RefreshWorker(*args, **kwargs):
    kwargs.setdefault("budget", OfflineBudget(max_reads=10000, max_writes=10000))
    from refresh_fixtures import strict_worker
    return strict_worker(_RefreshWorker, *args, **kwargs)
from test_job_coordinator import Clock, payload
from app.storage_analysis.planning import Estimate


class SchedulerTests(unittest.TestCase):
    def test_fixed_weekly_dispatch_and_missed_policy(self):
        clock=Clock(); schedule=Schedule('weekly','v1',clock.now,enabled=True)
        self.assertEqual(schedule.next_after(clock.now),clock.now+timedelta(days=7))
        scheduler=FakeScheduler(schedule)
        first=scheduler.dispatch(clock.now,active_jobs=0,budget_ok=True)
        self.assertIsNone(scheduler.dispatch(clock.now,active_jobs=0,budget_ok=True))
        clock.advance(21*86400+1)
        latest=scheduler.dispatch(clock.now,active_jobs=0,budget_ok=True)
        self.assertEqual(latest['scheduled_at'],schedule.anchor+timedelta(days=21))
        self.assertNotEqual(latest['occurrence_id'],first['occurrence_id'])
        self.assertIsNone(scheduler.dispatch(clock.now,active_jobs=0,budget_ok=True))

    def test_disabled_pause_stop_cooldown_and_limits(self):
        clock=Clock(); disabled=FakeScheduler(Schedule('s','v',clock.now))
        self.assertIsNone(disabled.dispatch(clock.now,active_jobs=0,budget_ok=True))
        scheduler=FakeScheduler(Schedule('s','v',clock.now,interval=timedelta(seconds=1),cooldown=timedelta(seconds=5),enabled=True))
        self.assertIsNone(scheduler.dispatch(clock.now,active_jobs=1,budget_ok=True))
        self.assertIsNone(scheduler.dispatch(clock.now,active_jobs=0,budget_ok=False))
        scheduler.pause(); self.assertIsNone(scheduler.dispatch(clock.now,active_jobs=0,budget_ok=True)); scheduler.resume()
        self.assertIsNotNone(scheduler.dispatch(clock.now,active_jobs=0,budget_ok=True))
        clock.advance(1); self.assertIsNone(scheduler.dispatch(clock.now,active_jobs=0,budget_ok=True))
        scheduler.stop(); self.assertIsNone(scheduler.dispatch(clock.now,active_jobs=0,budget_ok=True))
        with self.assertRaises(ValueError): scheduler.resume()
        with self.assertRaises(ValueError): Schedule('s','v',clock.now,max_catch_up=3)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock(); self.c=FakeCoordinator(self.clock); self.repo=FakeDatasetStore(self.c)
        self.policy=Policy(5,10,3,self.clock.now+timedelta(hours=3),30,10,2,Estimate(writes=4),Estimate(writes=10))
    def job(self,key): return self.c.claim(self.c.create(key,payload(key),self.policy).job_id,key)
    def state(self,j,target): return self.c.transition(j.job_id,j.owner,j.fencing,j.revision,target)
    def ready(self,j,token,name,quality=True):
        for s in (State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING): j=self.state(j,s)
        output=strict_output({'shards':{'s1':{'items':[1]}},'quality_ok':quality})
        metadata=dict(output['manifest_input'],occurrence_id=j.identity['occurrence_id'])
        report=dict(output['quality_report'],dataset_id=name)
        self.repo.draft(name,'stream',j.job_id,j.owner,j.fencing,j.revision,token,metadata)
        for key,shard in output['shards'].items():
            self.repo.put_shard(name,'stream',j.job_id,j.owner,j.fencing,j.revision,token,key,shard)
        self.repo.put_quality_report(name,'stream',j.job_id,j.owner,j.fencing,j.revision,token,report)
        manifest=build_offline_manifest(dataset_id=name,job_id=j.job_id,resource_id='stream',identity=j.identity,metadata=metadata,shards=output['shards'],quality_report=report,hash_fn=digest)
        self.repo.seal(name,'stream',j.job_id,j.owner,j.fencing,j.revision,token,manifest)
        j=self.state(j,State.VALIDATING)
        result=self.repo.verify(name,'stream',j.job_id,j.owner,j.fencing,j.revision,token,quality_ok=quality)
        return j,result

    def test_resource_fencing_two_distinct_occurrences_and_lost_ack(self):
        a=self.job('A'); ta=self.repo.claim_resource('stream',a.job_id,a.owner,a.fencing,a.revision)
        a,_=self.ready(a,ta,'draft-a')
        self.clock.advance(31)
        b=self.job('B'); tb=self.repo.claim_resource('stream',b.job_id,b.owner,b.fencing,b.revision)
        self.assertGreater(tb,ta)
        b,_=self.ready(b,tb,'draft-b')
        self.assertEqual(self.repo.publish('draft-b','stream',b.job_id,b.owner,b.fencing,b.revision,tb,None,0),'PUBLISHED')
        self.assertEqual(self.repo.publish('draft-b','stream',b.job_id,b.owner,b.fencing,b.revision,tb,None,0),'ALREADY_PUBLISHED')
        with self.assertRaises(LeaseLost): self.repo.publish('draft-a','stream',a.job_id,a.owner,a.fencing,a.revision,ta,None,0)
        self.assertEqual(self.repo.active('stream'),('draft-b',1))
        self.assertEqual(self.c.get(b.job_id).state,State.COMPLETED)

    def test_manifest_missing_digest_quality_and_immutability(self):
        j=self.job('J'); token=self.repo.claim_resource('stream',j.job_id,j.owner,j.fencing,j.revision)
        j,ok=self.ready(j,token,'data',quality=False)
        self.assertFalse(ok)
        with self.assertRaises(Conflict): self.repo.put_shard('data','stream',j.job_id,j.owner,j.fencing,j.revision,token,'s1',{'items':[2]})
        self.assertEqual(self.repo.active('stream'),(None,0))

    def test_pointer_cas_and_competing_claim(self):
        j=self.job('A'); token=self.repo.claim_resource('stream',j.job_id,j.owner,j.fencing,j.revision)
        j,_=self.ready(j,token,'data')
        with self.assertRaises(Conflict): self.repo.publish('data','stream',j.job_id,j.owner,j.fencing,j.revision,token,None,1)
        self.assertEqual(self.repo.active('stream'),(None,0))
        b=self.c.claim(self.c.create('B-other',dict(payload('B-other'),resource_id='other'),self.policy).job_id,'B'); barrier=Barrier(2)
        def claim(_):
            barrier.wait()
            try: return self.repo.claim_resource('other',b.job_id,b.owner,b.fencing,b.revision)
            except Conflict: return None
        with ThreadPoolExecutor(max_workers=2) as pool: values=list(pool.map(claim,range(2)))
        self.assertEqual(sum(v is not None for v in values),1)

    def test_distinct_jobs_compete_for_resource_concurrently(self):
        first=self.job('one'); second=self.job('two'); barrier=Barrier(2)
        def claim(job):
            barrier.wait()
            try: return self.repo.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
            except Conflict: return None
        with ThreadPoolExecutor(max_workers=2) as pool: result=list(pool.map(claim,[first,second]))
        self.assertEqual(sum(x is not None for x in result),1)

    def test_concurrent_publish_idempotence(self):
        j=self.job('A'); token=self.repo.claim_resource('stream',j.job_id,j.owner,j.fencing,j.revision)
        j,_=self.ready(j,token,'data')
        barrier=Barrier(2)
        def publish(_):
            barrier.wait()
            return self.repo.publish('data','stream',j.job_id,j.owner,j.fencing,j.revision,token,None,0)
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(publish,range(2)))
        self.assertEqual(sorted(results),['ALREADY_PUBLISHED','PUBLISHED'])
        self.assertEqual(self.repo.active('stream'),('data',1))

    def test_cancellation_at_checkpoint_does_not_publish(self):
        job=self.c.create('cancel',payload('cancel'),self.policy)
        def fetch():
            current=self.c.get(job.job_id)
            self.c.request_cancel(job.job_id,current.revision)
            return [1]
        worker=RefreshWorker(self.c,self.repo,fetch,lambda x:x,lambda x:x)
        self.assertEqual(worker.run(job.job_id,'owner','stream','dataset',expected_active=None,expected_revision=0),'CANCELLED')
        self.assertEqual(self.c.get(job.job_id).state,State.CANCELLED)
        self.assertEqual(self.repo.active('stream'),(None,0))

    def test_worker_happy_path_and_failed_refresh_preserves_active(self):
        job=self.c.create('worker',payload('worker'),self.policy)
        worker=RefreshWorker(self.c,self.repo,lambda:[1],lambda x:x,lambda x:{'metadata':{'source':'synthetic'},'shards':{'s':{'items':x}},'quality_ok':True})
        self.assertEqual(worker.run(job.job_id,'worker','stream','dataset',expected_active=None,expected_revision=0),'PUBLISHED')
        self.clock.advance(31)
        next_job=self.c.create('bad',payload('bad'),self.policy)
        bad=RefreshWorker(self.c,self.repo,lambda: (_ for _ in ()).throw(TimeoutError()),lambda x:x,lambda x:x)
        with self.assertRaises(TimeoutError): bad.run(next_job.job_id,'bad','stream','bad-draft',expected_active='dataset',expected_revision=1)
        self.assertEqual(self.repo.active('stream'),('dataset',1))
        self.assertEqual(self.c.get(next_job.job_id).state,State.FAILED)

if __name__=='__main__': unittest.main()
