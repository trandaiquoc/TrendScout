"""Scheduler failures must not silently lose an occurrence."""
import unittest
from datetime import timedelta
from app.scheduling.dispatch import dispatch_job
from app.scheduling.recurrence import FakeScheduler,Schedule
from app.jobs.fake import FakeCoordinator
from app.jobs.contracts import Policy
from app.storage_analysis.planning import Estimate
from app.workers.safety import OfflineBudget
from test_job_coordinator import Clock, payload


class DispatchTests(unittest.TestCase):
    def test_recreated_scheduler_cannot_duplicate_occurrence_with_changed_config(self):
        from app.jobs.contracts import Conflict
        clock=Clock(); schedule=Schedule('s','v1',clock.now,enabled=True); c=FakeCoordinator(clock)
        policy=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(100,100))
        first=dispatch_job(FakeScheduler(schedule),clock.now,active_jobs=0,budget_ok=True,coordinator=c,payload_factory=lambda o:payload(o['occurrence_id']),policy=policy)
        def changed(o): return dict(payload(o['occurrence_id']),config_version='v2')
        with self.assertRaises(Conflict):
            dispatch_job(FakeScheduler(schedule),clock.now,active_jobs=0,budget_ok=True,coordinator=c,payload_factory=changed,policy=policy)
        self.assertEqual(list(c._jobs),[first.job_id])

    def test_concurrent_dispatch_creates_one_job(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        clock=Clock(); scheduler=FakeScheduler(Schedule('s','v1',clock.now,enabled=True)); c=FakeCoordinator(clock)
        policy=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(100,100))
        gate=Barrier(2)
        def run():
            gate.wait()
            return dispatch_job(scheduler,clock.now,active_jobs=0,budget_ok=True,coordinator=c,payload_factory=lambda o:payload(o['occurrence_id']),policy=policy)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:run(),range(2)))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(len(c._jobs),1)

    def test_creation_failure_releases_occurrence_for_retry(self):
        clock=Clock(); scheduler=FakeScheduler(Schedule('s','v1',clock.now,enabled=True)); c=FakeCoordinator(clock)
        valid=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(100,100))
        def data(o): return payload(o['occurrence_id'])
        with self.assertRaises(ValueError): dispatch_job(scheduler,clock.now,active_jobs=0,budget_ok=True,coordinator=c,payload_factory=data,policy=None)
        job=dispatch_job(scheduler,clock.now,active_jobs=0,budget_ok=True,coordinator=c,payload_factory=data,policy=valid)
        self.assertIsNotNone(job)
