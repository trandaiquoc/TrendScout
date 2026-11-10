"""Refresh uses actual business logic and a strictly offline Wikimedia transport."""
from datetime import date, datetime, timedelta
from unittest import TestCase
from unittest.mock import patch
import httpx
from app.datasets.fake import FakeDatasetStore
from app.jobs.fake import FakeCoordinator, FakeTransactionalStore
from app.jobs.contracts import Policy, Conflict
from app.scheduling.recurrence import Schedule, FakeScheduler
from app.scheduling.dispatch import dispatch_job
from app.storage_analysis.planning import Estimate
from app.workers.offline_pipeline import OfflineWikimediaPipeline
from app.workers.refresh import RefreshWorker as _RefreshWorker
from app.workers.safety import OfflineBudget

def RefreshWorker(*args, **kwargs):
    kwargs.setdefault("budget", OfflineBudget(max_reads=10000, max_writes=10000))
    from refresh_fixtures import strict_worker
    return strict_worker(_RefreshWorker, *args, **kwargs)
from test_golden_contract import SOURCE, EXPECTED, assert_expected
from test_job_coordinator import Clock, payload


def transport():
    def handler(request):
        key=request.url.path[-10:].replace('/','-')
        entry=SOURCE['days'][key]
        if entry['http'] != 200: return httpx.Response(entry['http'],headers={'Retry-After':'0'})
        year,month,day=key.split('-')
        return httpx.Response(200,json={'items':[{**SOURCE['scope'],'year':year,'month':month,'day':day,'articles':entry['articles']}]})
    return httpx.MockTransport(handler)


def wikimedia_identity(occurrence):
    value=dict(payload(occurrence),source='wikimedia_pageviews_top_per_country')
    value['dataset_config']={'analysis_date':'2026-01-08','window_start':'2026-01-01','window_end':'2026-01-08',
        'source_version':'wikimedia-top-per-country-v1','processing_version':'cleaning-pipeline-v0.1',
        'cleaning_version':'cleaning-pipeline-v0.1','classification_version':'title-rules-v0.1','algorithm_version':'wikimedia-trend-v0.1'}
    return value


class OfflineIntegrationTests(TestCase):
    def test_scheduled_real_pipeline_golden_results(self):
        clock=Clock(); shared=FakeTransactionalStore(); jobs=FakeCoordinator(clock,shared)
        policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,3,Estimate(writes=4),Estimate(writes=10))
        schedule=Schedule('golden','v1',clock.now,enabled=True)
        def make_payload(occ): return wikimedia_identity(occ['occurrence_id'])
        job=dispatch_job(FakeScheduler(schedule),clock.now,active_jobs=0,budget_ok=True,coordinator=jobs,payload_factory=make_payload,policy=policy)
        again=dispatch_job(FakeScheduler(schedule),clock.now,active_jobs=0,budget_ok=True,coordinator=FakeCoordinator(clock,shared),payload_factory=make_payload,policy=policy)
        self.assertEqual(job.job_id,again.job_id)
        with self.assertRaises(Conflict): jobs.create(job.idempotency_key,payload('different'),policy)
        pipeline=OfflineWikimediaPipeline(transport(),date.fromisoformat(SOURCE['analysis_date']),[date.fromisoformat(d) for d in sorted(SOURCE['days'],reverse=True)])
        datasets=FakeDatasetStore(jobs)
        worker=RefreshWorker(jobs,datasets,pipeline.fetch,pipeline.clean,pipeline.analyze)
        fixed=datetime.fromisoformat(SOURCE['fetched_at'])
        with patch('app.sources.wikimedia.adapter.datetime',wraps=datetime) as frozen:
            frozen.now.return_value=fixed
            result=worker.run(job.job_id,'worker','stream','golden-dataset',expected_active=None,expected_revision=0)
        self.assertEqual(result,'PUBLISHED')
        self.assertEqual(datasets.active('stream'),('golden-dataset',1))
        persisted=datasets.read_dataset('golden-dataset')
        items=persisted['shards']['scores-v1']['items']
        actual={row['keyword_id']:row for row in items}
        self.assertEqual(len(actual),len(EXPECTED['entities']))
        for expected in EXPECTED['entities']: assert_expected(actual[expected['keyword_id']],expected)
        self.assertTrue(any(row['score'] is None for row in items))
        self.assertEqual(persisted['state'],'ACTIVE')
        snapshots=persisted['shards']['snapshots-v1']['items']
        raw=persisted['shards']['raw-observations-v1']['items']
        self.assertEqual(len(snapshots),len(raw))
        unavailable=next(x for x in snapshots if x['fetch_status']=='DAY_UNAVAILABLE')
        self.assertIsNone(unavailable['item_count'])
        current=next(x for x in raw if x['date']==SOURCE['analysis_date'])
        self.assertTrue(any(r['article_original']=='Main_Page' for r in current['rows']))
        self.assertTrue(any(r['views_ceil']==0 for r in current['rows']))

    def test_network_transport_is_rejected(self):
        with self.assertRaises(ValueError): OfflineWikimediaPipeline(httpx.AsyncHTTPTransport(),date(2026,1,8),[date(2026,1,8)])

    def test_lease_expires_at_each_pipeline_boundary(self):
        for boundary in ('before_fetch','after_fetch','before_clean','after_clean','before_analysis','after_analysis','before_draft','after_draft','before_shard','before_seal','after_seal','before_verify','before_publish'):
            with self.subTest(boundary=boundary):
                clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
                policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate())
                job=jobs.create('key',payload(),policy)
                worker=RefreshWorker(jobs,repo,lambda:[1],lambda x:x,lambda x:{'metadata':{'source':'synthetic'},'shards':{'one':{'items':x}},'quality_ok':True},checkpoint_hook=lambda name: clock.advance(31) if name==boundary else None)
                from app.jobs.contracts import LeaseLost
                with self.assertRaises(LeaseLost): worker.run(job.job_id,'worker','stream','draft',expected_active=None,expected_revision=0)
                self.assertEqual(repo.active('stream'),(None,0))
                self.assertNotEqual(jobs.get(job.job_id).state,'COMPLETED')

    def test_fake_restart_after_partial_draft_uses_new_fenced_namespace(self):
        class SimulatedCrash(BaseException): pass
        clock=Clock(); shared=FakeTransactionalStore(); jobs=FakeCoordinator(clock,shared); repo=FakeDatasetStore(jobs)
        policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate())
        job=jobs.create('crash',payload('crash'),policy)
        data=lambda x:{'metadata':{'source':'synthetic'},'shards':{'one':{'items':x}},'quality_ok':True}
        def fail(name):
            if name=='before_seal': raise SimulatedCrash()
        worker=RefreshWorker(jobs,repo,lambda:[1],lambda x:x,data,checkpoint_hook=fail)
        with self.assertRaises(SimulatedCrash): worker.run(job.job_id,'a','stream','draft',expected_active=None,expected_revision=0,fresh_on_retry=True)
        self.assertEqual(repo.read_dataset('draft-f1')['state'],'DRAFT')
        self.assertEqual(repo.active('stream'),(None,0))
        clock.advance(31)
        jobs2=FakeCoordinator(clock,shared)
        # Repository object is retained as fake backend; it is not durable process storage.
        repo.c=jobs2
        retry=RefreshWorker(jobs2,repo,lambda:[1],lambda x:x,data)
        self.assertEqual(retry.run(job.job_id,'b','stream','draft',expected_active=None,expected_revision=0,fresh_on_retry=True),'PUBLISHED')
        self.assertEqual(repo.active('stream'),('draft-f2',1))
        self.assertEqual(repo.read_dataset('draft-f1')['state'],'DRAFT')

    def test_source_unavailability_does_not_publish_zero_views(self):
        for status in (404,429,500):
            with self.subTest(status=status):
                mock=httpx.MockTransport(lambda request:httpx.Response(status,headers={'Retry-After':'0'}))
                clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
                policy=Policy(2,20,1,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate())
                job=jobs.create(str(status),payload(str(status)),policy)
                pipeline=OfflineWikimediaPipeline(mock,date(2026,1,8),[date(2026,1,8)])
                worker=RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze)
                with self.assertRaises(ValueError): worker.run(job.job_id,'worker','stream','data',expected_active=None,expected_revision=0)
                self.assertEqual(repo.active('stream'),(None,0))
                self.assertEqual(jobs.get(job.job_id).state,'FAILED')

    def test_http_429_retry_after_enforced_with_fake_clock(self):
        from app.workers.safety import RetryPolicy, RefreshError
        clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
        policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate())
        job=jobs.create('rate',payload('rate'),policy)
        source=OfflineWikimediaPipeline(httpx.MockTransport(lambda req:httpx.Response(429,headers={'Retry-After':'12'})),date(2026,1,8),[date(2026,1,8)],clock=clock)
        worker=RefreshWorker(jobs,repo,source.fetch,source.clean,source.analyze,retry_policy=RetryPolicy(3,2,30))
        with self.assertRaises(RefreshError): worker.run(job.job_id,'w','stream','d',expected_active=None,expected_revision=0)
        self.assertEqual(jobs.get(job.job_id).retry_after,clock.now+timedelta(seconds=12))
        clock.advance(11)
        with self.assertRaises(Conflict): jobs.claim(job.job_id,'w2')
        clock.advance(1)
        self.assertEqual(jobs.claim(job.job_id,'w2').attempts,2)

    def test_retry_after_http_date_uses_fixed_clock(self):
        from email.utils import format_datetime
        from app.workers.safety import RefreshError
        clock=Clock(); header=format_datetime(clock.now+timedelta(seconds=17),usegmt=True)
        source=OfflineWikimediaPipeline(httpx.MockTransport(lambda req:httpx.Response(429,headers={'Retry-After':header})),date(2026,1,8),[date(2026,1,8)],clock=clock)
        with self.assertRaises(RefreshError) as caught: source.fetch()
        self.assertEqual(caught.exception.retry_after,17)

    def test_source_request_budget_stops_before_next_http_call(self):
        calls=[]
        def handler(req):
            calls.append(str(req.url))
            day=req.url.path[-10:].replace('/','-'); entry=SOURCE['days'][day]
            year,month,daypart=day.split('-')
            return httpx.Response(200,json={'items':[{**SOURCE['scope'],'year':year,'month':month,'day':daypart,'articles':entry['articles']}]})
        clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
        policy=Policy(3,1,2,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate())
        job=jobs.create('limited',payload('limited'),policy)
        pipeline=OfflineWikimediaPipeline(httpx.MockTransport(handler),date(2026,1,8),[date(2026,1,8),date(2026,1,7)],clock=clock)
        from app.jobs.contracts import BudgetExceeded
        with self.assertRaises(BudgetExceeded): RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze).run(job.job_id,'w','stream','d',expected_active=None,expected_revision=0)
        self.assertEqual(len(calls),1)
        self.assertEqual(repo.active('stream'),(None,0))

    def test_http_500_and_timeout_retry_then_publish(self):
        from app.workers.safety import RetryPolicy, RefreshError
        for failure in ('500','timeout'):
            with self.subTest(failure=failure):
                clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs); calls=[]
                policy=Policy(3,4,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate())
                job=jobs.create(failure,wikimedia_identity(failure),policy)
                def handler(req):
                    calls.append(1)
                    if len(calls)==1:
                        if failure=='timeout': raise httpx.ReadTimeout('synthetic',request=req)
                        return httpx.Response(500)
                    return httpx.Response(200,json={'items':[{**SOURCE['scope'],'year':'2026','month':'01','day':'08','articles':SOURCE['days']['2026-01-08']['articles']}]})
                pipeline=OfflineWikimediaPipeline(httpx.MockTransport(handler),date(2026,1,8),[date(2026,1,8)],clock=clock)
                worker=RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze,retry_policy=RetryPolicy(3,2,10))
                with self.assertRaises(RefreshError): worker.run(job.job_id,'w1','stream','dataset',expected_active=None,expected_revision=0,fresh_on_retry=True)
                clock.advance(2)
                result=worker.run(job.job_id,'w2','stream','dataset',expected_active=None,expected_revision=0,fresh_on_retry=True)
                self.assertEqual(result,'PUBLISHED')
                self.assertEqual((len(calls),repo.active('stream')),(2,('dataset-f2',1)))

    def test_persisted_evidence_tampering_rejects_even_if_hashes_updated(self):
        from app.datasets.fake import digest
        for kind in ('missing_raw','raw_count','snapshot_date','snapshot_status','provenance','forged_quality','config_version','algorithm_version'):
            with self.subTest(kind=kind):
                clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
                policy=Policy(3,20,2,clock.now+timedelta(hours=1),30,10,3,Estimate(),Estimate())
                job=jobs.create(kind,wikimedia_identity(kind),policy)
                pipeline=OfflineWikimediaPipeline(transport(),date.fromisoformat(SOURCE['analysis_date']),[date.fromisoformat(d) for d in sorted(SOURCE['days'],reverse=True)])
                def mutate(point):
                    if point!='before_verify': return
                    d=repo.datasets['dataset']; m=d['manifest']; report=d['quality_report']; snap=d['shards']['snapshots-v1']['items'][0]
                    if kind=='missing_raw': del d['shards']['raw-observations-v1']
                    if kind=='raw_count': d['shards']['raw-observations-v1']['items'][0]['rows'].pop()
                    if kind=='snapshot_date': snap['observation_date']='2026-01-07'
                    if kind=='snapshot_status': snap['fetch_status']='DAY_UNAVAILABLE'
                    if kind=='provenance': snap['request_url']='https://invalid.example/path'
                    if kind in ('config_version','algorithm_version'):
                        m[kind]='bad-version'; d['metadata'][kind]='bad-version'
                    if kind=='forged_quality':
                        report['snapshots'][0]['accepted_count']+=1
                        report['snapshots'][0]['rejected_count']-=1
                        # An attacker also changes copies and digest: replay must still disagree.
                        m['quality_report']=report.copy(); m['quality_report_digest']=digest(report)
                    for key,shard in d['shards'].items():
                        m['shards'][key]['sha256']=digest(shard)
                worker=RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze,checkpoint_hook=mutate)
                with self.assertRaises(ValueError): worker.run(job.job_id,'worker','stream','dataset',expected_active=None,expected_revision=0)
                self.assertEqual(repo.active('stream'),(None,0))

    def test_429_then_success_after_retry_after(self):
        from app.workers.safety import RetryPolicy, RefreshError
        clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs); calls=[]
        policy=Policy(3,4,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate())
        job=jobs.create('rate-e2e',wikimedia_identity('rate-e2e'),policy)
        def handler(req):
            calls.append(1)
            if len(calls)==1: return httpx.Response(429,headers={'Retry-After':'12'})
            return httpx.Response(200,json={'items':[{**SOURCE['scope'],'year':'2026','month':'01','day':'08','articles':SOURCE['days']['2026-01-08']['articles']}]})
        pipeline=OfflineWikimediaPipeline(httpx.MockTransport(handler),date(2026,1,8),[date(2026,1,8)],clock=clock)
        worker=RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze,retry_policy=RetryPolicy(3,2,30))
        with self.assertRaises(RefreshError): worker.run(job.job_id,'a','stream','d',expected_active=None,expected_revision=0,fresh_on_retry=True)
        clock.advance(11)
        with self.assertRaises(Conflict): jobs.claim(job.job_id,'b')
        clock.advance(1)
        self.assertEqual(worker.run(job.job_id,'b','stream','d',expected_active=None,expected_revision=0,fresh_on_retry=True),'PUBLISHED')
        self.assertEqual((len(calls),repo.active('stream')),(2,('d-f2',1)))

    def test_invalid_source_payload_is_permanent_and_preserves_active(self):
        from app.workers.safety import RetryPolicy, RefreshError, ErrorCode
        clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
        policy=Policy(3,4,2,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate())
        j=jobs.create('invalid',wikimedia_identity('invalid'),policy)
        pipeline=OfflineWikimediaPipeline(httpx.MockTransport(lambda req:httpx.Response(200,json={'invalid':True})),date(2026,1,8),[date(2026,1,8)],clock=clock)
        worker=RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze,retry_policy=RetryPolicy(3,2,30))
        with self.assertRaises(RefreshError) as caught: worker.run(j.job_id,'w','stream','d',expected_active=None,expected_revision=0)
        self.assertEqual(caught.exception.code,ErrorCode.INVALID_SOURCE_DATA)
        self.assertEqual(jobs.get(j.job_id).state,'FAILED')
        self.assertEqual(repo.active('stream'),(None,0))

    def test_retry_exhausted_preserves_previous_active(self):
        from app.workers.safety import RetryPolicy, RefreshError
        clock=Clock(); jobs=FakeCoordinator(clock); repo=FakeDatasetStore(jobs)
        policy=Policy(2,4,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate())
        baseline=jobs.create('baseline',payload('baseline'),policy)
        base=RefreshWorker(jobs,repo,lambda:[1],lambda x:x,lambda x:{'shards':{'s':{'items':x}},'quality_ok':True})
        self.assertEqual(base.run(baseline.job_id,'old','stream','active',expected_active=None,expected_revision=0),'PUBLISHED')
        clock.advance(31)
        job=jobs.create('fail',wikimedia_identity('fail'),policy)
        pipeline=OfflineWikimediaPipeline(httpx.MockTransport(lambda req:httpx.Response(500)),date(2026,1,8),[date(2026,1,8)],clock=clock)
        worker=RefreshWorker(jobs,repo,pipeline.fetch,pipeline.clean,pipeline.analyze,retry_policy=RetryPolicy(2,2,10))
        with self.assertRaises(RefreshError): worker.run(job.job_id,'a','stream','new',expected_active='active',expected_revision=1,fresh_on_retry=True)
        clock.advance(2)
        with self.assertRaises(RefreshError): worker.run(job.job_id,'b','stream','new',expected_active='active',expected_revision=1,fresh_on_retry=True)
        self.assertEqual(jobs.get(job.job_id).state,'FAILED')
        self.assertEqual(repo.active('stream'),('active',1))
