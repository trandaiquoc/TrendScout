"""Fail closed when a source identity cannot anchor strict publication."""
import unittest
from dataclasses import replace
from datetime import timedelta
from app.jobs.fake import FakeCoordinator
from app.jobs.contracts import Policy
from app.storage_analysis.planning import Estimate
from app.workers.safety import OfflineBudget
from test_job_coordinator import Clock, payload


class IdentityTests(unittest.TestCase):
    def test_source_scope_requires_all_explicit_fields(self):
        clock=Clock(); c=FakeCoordinator(clock)
        policy=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(100,100))
        for bad in ({'country':'US'}, {'country':'US','access':'all-access'}, {'country':'US','project':'all'}):
            with self.subTest(scope=bad), self.assertRaises(ValueError):
                c.create(str(bad),dict(payload(),scope=bad),policy)

    def test_analysis_config_is_required_before_job_creation(self):
        clock=Clock(); c=FakeCoordinator(clock)
        p=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(100,100))
        data=dict(payload(),scope={'country':'US','access':'all-access','project':'all'},resource_id='stream')
        data.pop('dataset_config',None)
        with self.assertRaises(ValueError): c.create('missing-config',data,p)

    def test_unknown_source_must_not_bypass_independent_evidence(self):
        from app.datasets.fake import FakeDatasetStore, digest
        from app.datasets.manifest import build_offline_manifest
        from app.jobs.contracts import State
        from refresh_fixtures import strict_output
        clock=Clock(); c=FakeCoordinator(clock); repo=FakeDatasetStore(c)
        p=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(100,100))
        identity=dict(payload(),source='unknown_source')
        job=c.claim(c.create('unknown',identity,p).job_id,'w')
        token=repo.claim_resource('stream',job.job_id,'w',job.fencing,job.revision)
        for state in (State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING):
            job=c.transition(job.job_id,'w',job.fencing,job.revision,state)
        output=strict_output({'shards':{'s':{'items':[1]}},'quality_ok':True})
        meta=dict(output['manifest_input'],source_id='unknown_source',occurrence_id=job.identity['occurrence_id'])
        report=dict(output['quality_report'],dataset_id='d',source_id='unknown_source')
        repo.draft('d','stream',job.job_id,'w',job.fencing,job.revision,token,meta)
        repo.put_shard('d','stream',job.job_id,'w',job.fencing,job.revision,token,'s',{'items':[1]})
        repo.put_quality_report('d','stream',job.job_id,'w',job.fencing,job.revision,token,report)
        manifest=build_offline_manifest(dataset_id='d',job_id=job.job_id,resource_id='stream',identity=job.identity,metadata=meta,shards={'s':{'items':[1]}},quality_report=report,hash_fn=digest)
        repo.seal('d','stream',job.job_id,'w',job.fencing,job.revision,token,manifest)
        job=c.transition(job.job_id,'w',job.fencing,job.revision,State.VALIDATING)
        self.assertFalse(repo.verify('d','stream',job.job_id,'w',job.fencing,job.revision,token,quality_ok=True))

    def test_legacy_alias_cannot_bypass_source_profile(self):
        from app.datasets.fake import FakeDatasetStore, digest
        from app.datasets.manifest import build_offline_manifest
        from app.jobs.contracts import State
        from refresh_fixtures import strict_output
        clock=Clock(); c=FakeCoordinator(clock); repo=FakeDatasetStore(c)
        p=Policy(2,10,1,clock.now+timedelta(hours=1),30,10,1,Estimate(),Estimate(),operation_budget=OfflineBudget(1000,1000))
        job=c.claim(c.create('legacy',dict(payload('legacy'),source='wikimedia'),p).job_id,'w'); token=repo.claim_resource('stream',job.job_id,'w',job.fencing,job.revision)
        for state in (State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING): job=c.transition(job.job_id,'w',job.fencing,job.revision,state)
        output=strict_output({'shards':{'s':{'items':[1]}},'quality_ok':True})
        output['manifest_input']['source_id']='wikimedia'
        output['quality_report']['source_id']='wikimedia'
        output['shards']['snapshots-v1']['items'][0]['source']='wikimedia'
        output['shards']['raw-observations-v1']['items'][0]['rows'][0]['source']='wikimedia'
        output['shards']['scores-v1']['items'][0]['source']='wikimedia'
        import hashlib,json
        parts=('wikimedia','en.wikipedia','Synthetic_article')
        output['shards']['scores-v1']['items'][0]['keyword_id']=hashlib.sha256(json.dumps(parts,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        meta=dict(output['manifest_input'],occurrence_id='legacy')
        report=dict(output['quality_report'],dataset_id='d')
        repo.draft('d','stream',job.job_id,'w',job.fencing,job.revision,token,meta)
        for key,value in output['shards'].items(): repo.put_shard('d','stream',job.job_id,'w',job.fencing,job.revision,token,key,value)
        repo.put_quality_report('d','stream',job.job_id,'w',job.fencing,job.revision,token,report)
        m=build_offline_manifest(dataset_id='d',job_id=job.job_id,resource_id='stream',identity=job.identity,metadata=meta,shards=output['shards'],quality_report=report,hash_fn=digest)
        repo.seal('d','stream',job.job_id,'w',job.fencing,job.revision,token,m)
        job=c.transition(job.job_id,'w',job.fencing,job.revision,State.VALIDATING)
        self.assertFalse(repo.verify('d','stream',job.job_id,'w',job.fencing,job.revision,token,quality_ok=True))
