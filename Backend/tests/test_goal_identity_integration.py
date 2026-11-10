"""Mutation tests against coordinator identity and persisted source evidence."""
import unittest
from datetime import date,timedelta
from app.jobs.fake import FakeCoordinator
from app.jobs.contracts import Policy
from app.datasets.fake import FakeDatasetStore
from app.storage_analysis.planning import Estimate
from app.workers.safety import OfflineBudget
from app.workers.offline_pipeline import OfflineWikimediaPipeline
from app.workers.refresh import RefreshWorker
from test_job_coordinator import Clock
from test_refresh_integration import SOURCE,transport,wikimedia_identity


class IdentityMutationTests(unittest.TestCase):
    def test_unscheduled_job_cannot_publish_invented_schedule_identity(self):
        clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
        policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
        job=jobs.create('unscheduled',wikimedia_identity('unscheduled'),policy)
        pipeline=OfflineWikimediaPipeline(transport(),date.fromisoformat(SOURCE['analysis_date']),[date.fromisoformat(x) for x in sorted(SOURCE['days'],reverse=True)],clock=clock)
        def mutate(point):
            if point=='before_verify':
                d=repo.datasets['dataset']
                d['manifest'].update(schedule_id='invented',schedule_version='v1')
                d['metadata'].update(schedule_id='invented',schedule_version='v1')
        worker=RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze,budget=OfflineBudget(10000,10000),checkpoint_hook=mutate)
        with self.assertRaises(ValueError): worker.run(job.job_id,'owner','stream','dataset',expected_active=None,expected_revision=0)
        self.assertEqual(repo.active('stream'),(None,0))

    def test_mutations_do_not_publish(self):
        fields=['job_id','occurrence_id','resource_id','source_id','country','access','project',
                'analysis_date','window_start','window_end','config_version','source_version','processing_version',
                'cleaning_version','classification_version','algorithm_version','provenance','schedule_id','schedule_version']
        for field in fields:
            with self.subTest(field=field):
                clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
                policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
                identity=wikimedia_identity(field); identity.update(schedule_id='schedule-1',schedule_version='v1')
                job=jobs.create(field,identity,policy)
                pipeline=OfflineWikimediaPipeline(transport(),date.fromisoformat(SOURCE['analysis_date']),[date.fromisoformat(x) for x in sorted(SOURCE['days'],reverse=True)],clock=clock)
                def mutate(point):
                    if point!='before_verify': return
                    d=repo.datasets['dataset']; d['manifest'][field]='altered'
                    if field in d['metadata']: d['metadata'][field]='altered'
                worker=RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze,budget=OfflineBudget(10000,10000),checkpoint_hook=mutate)
                with self.assertRaises(ValueError): worker.run(job.job_id,'owner','stream','dataset',expected_active=None,expected_revision=0)
                self.assertEqual(repo.active('stream'),(None,0))

    def test_unsupported_versions_cannot_self_attest(self):
        clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
        policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
        identity=wikimedia_identity('bad-algorithm'); identity['dataset_config']['algorithm_version']='bogus-v99'
        job=jobs.create('bad-algorithm',identity,policy)
        pipeline=OfflineWikimediaPipeline(transport(),date.fromisoformat(SOURCE['analysis_date']),[date.fromisoformat(x) for x in sorted(SOURCE['days'],reverse=True)],clock=clock)
        from app.datasets.fake import digest
        def mutate(point):
            if point!='before_verify': return
            d=repo.datasets['dataset']; d['manifest']['algorithm_version']='bogus-v99'; d['metadata']['algorithm_version']='bogus-v99'
            for row in d['shards']['scores-v1']['items']: row['algorithm_version']='bogus-v99'
            d['manifest']['shards']['scores-v1']['sha256']=digest(d['shards']['scores-v1'])
        worker=RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze,budget=OfflineBudget(10000,10000),checkpoint_hook=mutate)
        with self.assertRaises(ValueError): worker.run(job.job_id,'owner','stream','dataset',expected_active=None,expected_revision=0)
        self.assertEqual(repo.active('stream'),(None,0))

    def test_identity_change_after_verification_cannot_publish(self):
        clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
        policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate(),operation_budget=OfflineBudget(10000,10000))
        job=jobs.create('after-verify',wikimedia_identity('after-verify'),policy)
        pipeline=OfflineWikimediaPipeline(transport(),date.fromisoformat(SOURCE['analysis_date']),[date.fromisoformat(x) for x in sorted(SOURCE['days'],reverse=True)],clock=clock)
        def mutate(point):
            if point=='before_publish': jobs._jobs[job.job_id].identity['dataset_config']['algorithm_version']='changed'
        worker=RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze,budget=OfflineBudget(10000,10000),checkpoint_hook=mutate)
        from app.jobs.contracts import Conflict
        with self.assertRaises(Conflict): worker.run(job.job_id,'owner','stream','dataset',expected_active=None,expected_revision=0)
        self.assertEqual(repo.active('stream'),(None,0))
