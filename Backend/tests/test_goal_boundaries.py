"""Exhaustive synthetic budget-cutoff checks over actual worker operations."""
import unittest
from datetime import timedelta
from app.jobs.fake import FakeCoordinator
from app.jobs.contracts import Policy, State
from app.datasets.fake import FakeDatasetStore
from app.storage_analysis.planning import Estimate
from app.workers.safety import OfflineBudget, RefreshError
from app.workers.refresh import RefreshWorker as _RefreshWorker
from refresh_fixtures import strict_worker

def RefreshWorker(*args, **kwargs): return strict_worker(_RefreshWorker,*args,**kwargs)
from refresh_fixtures import strict_output
from test_job_coordinator import Clock,payload


def run(limit, read_limit=10000):
    clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs); reached=[]
    policy=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(read_limit,limit))
    job=jobs.create('job',payload('job'),policy)
    def output(rows): return strict_output({'shards':{'a':{'items':rows},'b':{'items':[]}},'quality_ok':True})
    worker=RefreshWorker(jobs,repo,lambda:[1],lambda x:x,output,budget=OfflineBudget(read_limit,limit),checkpoint_hook=reached.append)
    return jobs,repo,job,worker,reached


class BudgetBoundaryTests(unittest.TestCase):
    def test_publish_accounts_each_persisted_shard_read(self):
        from test_refresh_offline import PublicationTests
        fixture=PublicationTests(); fixture.setUp()
        job=fixture.job('publish-reads')
        token=fixture.repo.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
        job,ok=fixture.ready(job,token,'dataset'); self.assertTrue(ok)
        budget=fixture.c._jobs[job.job_id].refresh_budget
        budget.max_reads=budget.reads+3
        with self.assertRaises(RefreshError):
            fixture.repo.publish('dataset','stream',job.job_id,job.owner,job.fencing,job.revision,token,None,0)
        self.assertEqual(fixture.repo.active('stream'),(None,0))
        self.assertNotEqual(fixture.c._jobs[job.job_id].state,State.COMPLETED)

    def test_each_write_cutoff_cannot_publish(self):
        jobs,repo,job,worker,reached=run(10000)
        self.assertEqual(worker.run(job.job_id,'worker','stream','data',expected_active=None,expected_revision=0),'PUBLISHED')
        actual=jobs._jobs[job.job_id].refresh_budget.writes
        self.assertTrue({'before_fetch','after_draft','before_verify','before_publish'} <= set(reached))
        boundaries=set()
        for limit in range(2,actual):
            with self.subTest(limit=limit):
                j,r,record,w,steps=run(limit)
                try: w.run(record.job_id,'worker','stream','data',expected_active=None,expected_revision=0)
                except RefreshError: pass
                else: self.fail('operation exceeded budget without error')
                boundaries.update(steps)
                self.assertNotEqual(j._jobs[record.job_id].state,State.COMPLETED)
                self.assertEqual(r.active('stream'),(None,0))
        self.assertTrue({'before_fetch','after_draft','before_verify','before_publish'} <= boundaries)

    def test_each_read_cutoff_cannot_publish(self):
        jobs,repo,job,worker,_=run(10000)
        worker.run(job.job_id,'worker','stream','data',expected_active=None,expected_revision=0)
        actual=jobs._jobs[job.job_id].refresh_budget.reads
        for limit in range(2,actual):
            with self.subTest(limit=limit):
                j,r,record,w,_=run(10000,read_limit=limit)
                with self.assertRaises(RefreshError):
                    w.run(record.job_id,'worker','stream','data',expected_active=None,expected_revision=0)
                self.assertNotEqual(j._jobs[record.job_id].state,State.COMPLETED)
                self.assertEqual(r.active('stream'),(None,0))

    def test_write_cutoffs_preserve_existing_active(self):
        probe_jobs,_,probe,worker,_=run(10000)
        worker.run(probe.job_id,'worker','stream','data',expected_active=None,expected_revision=0)
        total=probe_jobs._jobs[probe.job_id].refresh_budget.writes
        for limit in range(2,total):
            with self.subTest(limit=limit):
                clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
                def policy(max_writes):
                    return Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,max_writes))
                def output(rows): return strict_output({'shards':{'a':{'items':rows},'b':{'items':[]}},'quality_ok':True})
                first=jobs.create('old',payload('old'),policy(10000))
                old=RefreshWorker(jobs,repo,lambda:[1],lambda x:x,output,budget=OfflineBudget(10000,10000))
                old.run(first.job_id,'old','stream','old-data',expected_active=None,expected_revision=0)
                clock.advance(31)
                new=jobs.create('new',payload('new'),policy(limit))
                worker=RefreshWorker(jobs,repo,lambda:[1],lambda x:x,output,budget=OfflineBudget(10000,limit))
                with self.assertRaises(RefreshError): worker.run(new.job_id,'new','stream','new-data',expected_active='old-data',expected_revision=1)
                self.assertEqual(repo.active('stream'),('old-data',1))
                self.assertNotEqual(jobs._jobs[new.job_id].state,State.COMPLETED)
