"""A blocked worker cannot alter state after another occurrence takes over."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from app.jobs.fake import FakeCoordinator
from app.jobs.contracts import Policy, LeaseLost
from app.datasets.fake import FakeDatasetStore
from app.storage_analysis.planning import Estimate
from app.workers.safety import OfflineBudget
from app.workers.refresh import RefreshWorker as _RefreshWorker
from refresh_fixtures import strict_worker

def RefreshWorker(*args, **kwargs): return strict_worker(_RefreshWorker,*args,**kwargs)
from refresh_fixtures import strict_output
from test_job_coordinator import Clock,payload


class ConcurrentTakeoverTests(unittest.TestCase):
    def test_shared_pipeline_does_not_use_replacement_guard_for_stale_worker(self):
        import httpx
        from datetime import date
        from app.workers.offline_pipeline import OfflineWikimediaPipeline
        from test_refresh_integration import SOURCE, wikimedia_identity
        clock=Clock(); jobs=FakeCoordinator(clock); store=FakeDatasetStore(jobs)
        p=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
        a=jobs.create('shared-a',wikimedia_identity('shared-a'),p); b=jobs.create('shared-b',wikimedia_identity('shared-b'),p)
        entered=threading.Event(); resume=threading.Event(); lock=threading.Lock(); calls=[]
        def handler(request):
            with lock: calls.append(1); first=len(calls)==1
            if first:
                entered.set()
                if not resume.wait(3): raise RuntimeError('test timeout')
            day=request.url.path[-10:].replace('/','-'); entry=SOURCE['days'][day]
            year,month,part=day.split('-')
            return httpx.Response(200,json={'items':[{**SOURCE['scope'],'year':year,'month':month,'day':part,'articles':entry['articles']}]})
        source=OfflineWikimediaPipeline(httpx.MockTransport(handler),date(2026,1,8),[date(2026,1,8),date(2026,1,7)],clock=clock)
        def worker(): return _RefreshWorker(jobs,store,source.fetch,source.clean,source.analyze,budget=OfflineBudget(10000,10000))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(worker().run,a.job_id,'a','stream','old',expected_active=None,expected_revision=0,fresh_on_retry=True)
            try:
                self.assertTrue(entered.wait(3)); clock.advance(31)
                self.assertEqual(worker().run(b.job_id,'b','stream','new',expected_active=None,expected_revision=0,fresh_on_retry=True),'PUBLISHED')
                budget=jobs._jobs[b.job_id].refresh_budget; before=(budget.reads,budget.writes,budget.source_requests)
            finally: resume.set()
            with self.assertRaises(LeaseLost): future.result(timeout=3)
        self.assertEqual((budget.reads,budget.writes,budget.source_requests),before)
        self.assertEqual(store.active('stream'),('new-f2',1))

    def test_slow_fetch_stale_worker_does_not_publish(self):
        clock=Clock(); jobs=FakeCoordinator(clock); store=FakeDatasetStore(jobs)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
        a=jobs.create('a',payload('a'),p); b=jobs.create('b',payload('b'),p)
        entered=threading.Event(); resume=threading.Event()
        def fetch_a():
            entered.set()
            if not resume.wait(3): raise RuntimeError('test timeout')
            return [1]
        def output(rows): return strict_output({'shards':{'a':{'items':rows}},'quality_ok':True})
        worker_a=RefreshWorker(jobs,store,fetch_a,lambda x:x,output,budget=OfflineBudget(10000,10000))
        worker_b=RefreshWorker(jobs,store,lambda:[1],lambda x:x,output,budget=OfflineBudget(10000,10000))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(worker_a.run,a.job_id,'a','stream','old',expected_active=None,expected_revision=0,fresh_on_retry=True)
            try:
                self.assertTrue(entered.wait(3)); clock.advance(31)
                self.assertEqual(worker_b.run(b.job_id,'b','stream','new',expected_active=None,expected_revision=0,fresh_on_retry=True),'PUBLISHED')
            finally: resume.set()
            with self.assertRaises(LeaseLost): future.result(timeout=3)
        self.assertEqual(store.active('stream'),('new-f2',1))

    def test_cancel_and_publish_atomic_race(self):
        from threading import Barrier
        from app.jobs.contracts import Conflict, InvalidTransition
        from test_refresh_offline import PublicationTests
        for iteration in range(20):
            with self.subTest(iteration=iteration):
                fixture=PublicationTests(); fixture.setUp()
                job=fixture.job('race'); token=fixture.repo.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
                job,ok=fixture.ready(job,token,'dataset'); self.assertTrue(ok)
                barrier=Barrier(2)
                def publish():
                    barrier.wait()
                    try: return fixture.repo.publish('dataset','stream',job.job_id,job.owner,job.fencing,job.revision,token,None,0)
                    except Conflict: return 'CONFLICT'
                def cancel():
                    barrier.wait()
                    try: return fixture.c.request_cancel(job.job_id,job.revision).state.value
                    except (Conflict,InvalidTransition): return 'CONFLICT'
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f1=pool.submit(publish); f2=pool.submit(cancel); a,b=f1.result(),f2.result()
                active=fixture.repo.active('stream')
                if a=='PUBLISHED': self.assertEqual((b,active),('CONFLICT',('dataset',1)))
                else: self.assertEqual((a,b,active),('CONFLICT','CANCELLING',(None,0)))

    def test_two_repository_clients_share_resource_fencing(self):
        from app.jobs.fake import FakeTransactionalStore
        from app.jobs.contracts import Conflict
        from threading import Barrier
        clock=Clock(); backing=FakeTransactionalStore(); c1=FakeCoordinator(clock,backing); c2=FakeCoordinator(clock,backing)
        r1=FakeDatasetStore(c1); r2=FakeDatasetStore(c2)
        p=Policy(3,10,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
        a=c1.claim(c1.create('a',payload('a'),p).job_id,'a'); b=c2.claim(c2.create('b',payload('b'),p).job_id,'b')
        barrier=Barrier(2)
        def claim(pair):
            store,job=pair; barrier.wait()
            try: return store.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
            except Conflict: return None
        with ThreadPoolExecutor(max_workers=2) as pool: result=list(pool.map(claim,[(r1,a),(r2,b)]))
        self.assertEqual(sum(x is not None for x in result),1)
