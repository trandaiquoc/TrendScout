"""Retention exercises only the in-process Step 14 fake repository."""
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

from app.retention.offline import FakeCleanup, STRICT_RETENTION, GRACE_FALLBACK, Limits
import test_refresh_offline


def publish_standard(f, key, *, claimed=None):
    from app.jobs.contracts import State
    from app.datasets.fake import digest
    from app.datasets.manifest import build_offline_manifest
    from refresh_fixtures import strict_output
    job=claimed if claimed is not None else f.job(key)
    token=f.repo.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
    for target in (State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING): job=f.state(job,target)
    output=strict_output({'shards':{},'quality_ok':True})
    metadata=dict(output['manifest_input'],occurrence_id=job.identity['occurrence_id'])
    report=dict(output['quality_report'],dataset_id='old')
    f.repo.draft('old','stream',job.job_id,job.owner,job.fencing,job.revision,token,metadata)
    for name,value in output['shards'].items(): f.repo.put_shard('old','stream',job.job_id,job.owner,job.fencing,job.revision,token,name,value)
    f.repo.put_quality_report('old','stream',job.job_id,job.owner,job.fencing,job.revision,token,report)
    manifest=build_offline_manifest(dataset_id='old',job_id=job.job_id,resource_id='stream',identity=job.identity,metadata=metadata,shards=output['shards'],quality_report=report,hash_fn=digest)
    f.repo.seal('old','stream',job.job_id,job.owner,job.fencing,job.revision,token,manifest)
    job=f.state(job,State.VALIDATING)
    if not f.repo.verify('old','stream',job.job_id,job.owner,job.fencing,job.revision,token,quality_ok=None): raise AssertionError('fixture verification failed')
    f.repo.publish('old','stream',job.job_id,job.owner,job.fencing,job.revision,token,None,0)
    return job,token


class RetentionTests(unittest.TestCase):
    def test_constructing_dry_run_does_not_mutate_fake_store(self):
        from app.jobs.fake import FakeCoordinator,FakeTransactionalStore
        from app.datasets.fake import FakeDatasetStore
        from test_job_coordinator import Clock
        shared=FakeTransactionalStore(); clock=Clock(); repo=FakeDatasetStore(FakeCoordinator(clock,shared))
        before={k:deepcopy(v) for k,v in vars(shared).items() if k!='mutex'}
        FakeCleanup(repo,clock)
        self.assertEqual({k:v for k,v in vars(shared).items() if k!='mutex'},before)

    def setUp(self):
        self.f=test_refresh_offline.PublicationTests(); self.f.setUp()
        publish_standard(self.f,'retention-old')
        self.cleanup=FakeCleanup(self.f.repo,self.f.clock)
        self.cleanup.set_protection('stream','old',license_clear=True)

    def test_dry_run_keeps_active_and_all_data(self):
        self.f.clock.advance(8*86400)
        self.cleanup.set_protection('stream','old',refresh_failed=True)
        before=deepcopy((self.f.repo.datasets,self.f.repo.resources))
        result=self.cleanup.plan('stream','old',GRACE_FALLBACK)
        self.assertEqual(result.reason,'ACTIVE_GRACE')
        self.assertEqual(result.freshness,'STALE')
        self.assertEqual((self.f.repo.datasets,self.f.repo.resources),before)

    def test_grace_requires_recorded_refresh_failure(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',GRACE_FALLBACK)
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason,'ACTIVE_GRACE_UNVERIFIED')
        self.assertNotEqual(plan.freshness,'STALE')

    def test_strict_fake_execution_unpoints_active_before_deleting(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        self.assertTrue(plan.operator_approval_required)
        self.assertTrue(plan.eligible)
        self.assertEqual(self.f.repo.active('stream'),('old',1))
        execution=self.cleanup.start(plan,execute_fake=True)
        self.assertEqual(self.f.repo.active('stream'),(None,2))
        self.assertEqual(self.f.repo.datasets['old']['state'],'CLEANING')
        for _ in range(30):
            if execution.state=='COMPLETED': break
            execution=self.cleanup.step(plan,execution,max_deletes=2)
        self.assertEqual(execution.state,'COMPLETED')
        self.assertNotIn('old',self.f.repo.datasets)
        self.assertEqual(self.f.repo.active('stream'),(None,2))

    def test_default_start_is_dry_run(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        before=deepcopy((self.f.repo.datasets,self.f.repo.resources))
        self.assertEqual(self.cleanup.start(plan).state,'PLANNED')
        self.assertEqual((self.f.repo.datasets,self.f.repo.resources),before)

    def test_truthy_string_cannot_enable_fake_deletion(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        before=deepcopy((self.f.repo.datasets,self.f.repo.resources))
        with self.assertRaises(ValueError): self.cleanup.start(plan,execute_fake='false')
        self.assertEqual((self.f.repo.datasets,self.f.repo.resources),before)

    def test_forged_execution_plan_cannot_complete_with_children(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        collision=replace(plan,paths=('analysis_datasets/old2/quality/report',))
        with self.assertRaises(ValueError): self.cleanup.start(collision,execute_fake=True)
        self.assertEqual(self.f.repo.active('stream'),('old',1))
        checkpoint=self.cleanup.start(plan,execute_fake=True)
        forged=replace(plan,paths=('analysis_datasets/old/quality/report',))
        with self.assertRaises(ValueError): self.cleanup.step(forged,checkpoint)
        self.assertEqual(checkpoint.state,'READY')
        self.assertTrue(self.f.repo.datasets['old']['shards'])

    def test_pointer_change_during_execution_stops_before_next_delete(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        checkpoint=self.cleanup.start(plan,execute_fake=True)
        before=deepcopy(self.f.repo.datasets['old'])
        self.f.repo.resources['stream']['pointer_revision']+=1
        with self.assertRaises(ValueError): self.cleanup.step(plan,checkpoint)
        self.assertEqual(self.f.repo.datasets['old'],before)
        self.assertGreater(self.cleanup.storage['runs'][plan.plan_id].reads,checkpoint.reads)
        self.assertGreater(self.cleanup.storage['runs'][plan.plan_id].writes,checkpoint.writes)

    def test_hold_shared_and_baseline_references_block_cleanup(self):
        self.f.clock.advance(8*86400)
        for kind in ('hold','shared','baseline'):
            with self.subTest(kind=kind):
                self.cleanup.set_protection('stream','old',**{kind:True})
                plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
                self.assertFalse(plan.eligible)
                self.assertIn(plan.reason,('ON_HOLD','PROTECTED_REFERENCE'))
                with self.assertRaises(ValueError): self.cleanup.start(plan,execute_fake=True)
                self.cleanup.set_protection('stream','old',**{kind:False})

    def test_protection_added_after_planning_invalidates_plan(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        self.cleanup.set_protection('stream','old',shared=True)
        with self.assertRaises(ValueError): self.cleanup.start(plan,execute_fake=True)
        self.assertEqual(self.f.repo.active('stream'),('old',1))

    def test_quota_stops_after_bounded_deletes(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION,limits=Limits(deletes=2))
        checkpoint=self.cleanup.start(plan,execute_fake=True)
        checkpoint=self.cleanup.step(plan,checkpoint)
        self.assertEqual(checkpoint.deletes,2)
        paused=self.cleanup.step(plan,checkpoint)
        self.assertEqual(paused.state,'PAUSED')
        self.assertEqual(paused.deletes,2)
        self.assertFalse(paused.inflight)
        self.assertIn('old',self.f.repo.datasets)

    def test_license_unknown_and_legal_hold_conflict_fail_closed(self):
        self.f.clock.advance(8*86400)
        self.cleanup.set_protection('stream','old',license_clear=False)
        self.assertEqual(self.cleanup.plan('stream','old',STRICT_RETENTION).reason,'LICENSE_UNKNOWN')
        self.cleanup.set_protection('stream','old',license_clear=True,legal_delete=True,hold=True)
        self.assertEqual(self.cleanup.plan('stream','old',STRICT_RETENTION).reason,'POLICY_CONFLICT')

    def test_legal_delete_overrides_grace_when_no_hold(self):
        self.cleanup.set_protection('stream','old',legal_delete=True)
        plan=self.cleanup.plan('stream','old',GRACE_FALLBACK)
        self.assertTrue(plan.eligible)
        self.assertEqual(plan.reason,'ELIGIBLE_LEGAL')
        self.cleanup.start(plan,execute_fake=True)
        self.assertEqual(self.f.repo.active('stream'),(None,2))

    def test_traversal_wrong_identity_and_unknown_subcollection_fail_closed(self):
        self.f.clock.advance(8*86400)
        for resource,dataset in (('stream','../old'),('stream','old/extra'),('stream','other'),('streams','old'),('users','old')):
            with self.subTest(resource=resource,dataset=dataset), self.assertRaises(ValueError):
                self.cleanup.plan(resource,dataset,STRICT_RETENTION)
        self.f.repo.datasets['old']['unknown_subcollection']={'secret':'untouched'}
        with self.assertRaises(ValueError): self.cleanup.plan('stream','old',STRICT_RETENTION)
        self.assertEqual(self.f.repo.datasets['old']['unknown_subcollection'],{'secret':'untouched'})

    def test_changed_remaining_shard_cannot_be_deleted(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        checkpoint=self.cleanup.start(plan,execute_fake=True)
        name=next(iter(self.f.repo.datasets['old']['shards']))
        self.f.repo.datasets['old']['shards'][name]['items'].append('tampered')
        with self.assertRaises(ValueError): self.cleanup.step(plan,checkpoint)
        self.assertIn(name,self.f.repo.datasets['old']['shards'])

    def test_resource_fencing_change_invalidates_plan(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        self.f.repo.resources['stream']['token']+=1
        with self.assertRaises(ValueError): self.cleanup.start(plan,execute_fake=True)
        self.assertEqual(self.f.repo.active('stream'),('old',1))

    def test_changed_authoritative_job_identity_rejects_cleanup(self):
        self.f.clock.advance(8*86400)
        dataset=self.f.repo.datasets['old']
        self.f.c._jobs[dataset['job_id']].identity['dataset_config']['algorithm_version']='forged'
        with self.assertRaises(ValueError): self.cleanup.plan('stream','old',STRICT_RETENTION)
        self.assertEqual(self.f.repo.active('stream'),('old',1))

    def test_crash_matrix_reload_preserves_receipts_and_does_not_repeat_deletes(self):
        import pickle, tempfile
        from pathlib import Path
        from app.jobs.fake import FakeCoordinator, FakeTransactionalStore
        from app.datasets.fake import FakeDatasetStore
        class Crash(BaseException): pass
        for boundary in ('before_plan','after_plan','after_validation','before_batch','after_delete',
                         'before_checkpoint','after_checkpoint','before_parent','after_parent','before_ack'):
            with self.subTest(boundary=boundary):
                self.setUp(); self.f.clock.advance(8*86400); fired=[]
                def hook(point):
                    if point==boundary and not fired:
                        fired.append(point); raise Crash()
                cleanup=FakeCleanup(self.f.repo,self.f.clock,fault_hook=hook)
                plan=None
                try:
                    plan=cleanup.plan('stream','old',STRICT_RETENTION)
                    checkpoint=cleanup.start(plan,execute_fake=True)
                    for _ in range(30):
                        if checkpoint.state=='COMPLETED': break
                        checkpoint=cleanup.step(plan,checkpoint,max_deletes=1)
                except Crash: pass
                self.assertEqual(fired,[boundary])
                shared=self.f.c._store
                state={k:getattr(shared,k) for k in ('jobs','keys','occurrences','resources','datasets','publications','retention_state')}
                with tempfile.TemporaryDirectory() as tmp:
                    file=Path(tmp)/'trusted-fake'; file.write_bytes(pickle.dumps(state))
                    restored=pickle.loads(file.read_bytes())
                new=FakeTransactionalStore()
                for key,value in restored.items(): setattr(new,key,value)
                repo=FakeDatasetStore(FakeCoordinator(self.f.clock,new)); resumed=FakeCleanup(repo,self.f.clock)
                if plan is None: plan=resumed.plan('stream','old',STRICT_RETENTION)
                checkpoint=resumed.start(plan,execute_fake=True)
                for _ in range(30):
                    if checkpoint.state=='COMPLETED': break
                    if checkpoint.state=='RETRY_WAIT': self.f.clock.advance(1)
                    checkpoint=resumed.step(plan,checkpoint,max_deletes=1)
                self.assertEqual(checkpoint.state,'COMPLETED')
                self.assertEqual(checkpoint.completed_paths,list(plan.paths))
                self.assertEqual(checkpoint.deletes,len(plan.paths))
                self.assertNotIn('old',repo.datasets)
                self.assertEqual(repo.active('stream'),(None,2))
                self.assertEqual(resumed.step(plan,checkpoint).deletes,len(plan.paths))

    def test_pause_resume_and_bounded_retry_do_not_reset_budget(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION,limits=Limits(retries=1))
        state=self.cleanup.start(plan,execute_fake=True)
        state=self.cleanup.step(plan,state,max_deletes=1)
        before=state.deletes
        state=self.cleanup.pause(plan)
        self.assertEqual(self.cleanup.step(plan,state).deletes,before)
        state=self.cleanup.resume(plan)
        state=self.cleanup.schedule_retry(plan,error_code='TRANSIENT_FAKE_ERROR')
        self.assertEqual(state.state,'RETRY_WAIT')
        self.assertEqual(self.cleanup.step(plan,state).deletes,before)
        self.f.clock.advance(1)
        state=self.cleanup.step(plan,state,max_deletes=1)
        self.assertEqual(state.deletes,before+1)
        state=self.cleanup.schedule_retry(plan,error_code='TRANSIENT_FAKE_ERROR')
        self.assertEqual(state.state,'FAILED')
        self.assertEqual(state.retries,1)
        self.assertEqual(self.cleanup.step(plan,state).deletes,before+1)

    def test_nonexpired_and_grace_expiry(self):
        self.assertEqual(self.cleanup.plan('stream','old',STRICT_RETENTION).reason,'NOT_EXPIRED')
        self.f.clock.advance(14*86400)
        plan=self.cleanup.plan('stream','old',GRACE_FALLBACK)
        self.assertTrue(plan.eligible)
        self.cleanup.start(plan,execute_fake=True)
        self.assertEqual(self.f.repo.active('stream'),(None,2))

    def test_old_job_cannot_expire_a_just_published_dataset(self):
        from test_job_coordinator import payload
        f=test_refresh_offline.PublicationTests(); f.setUp()
        f.policy=replace(f.policy,deadline=f.clock.now+timedelta(days=30))
        queued=f.c.create('old-job',payload('old-job'),f.policy)
        f.clock.advance(8*86400)
        job=f.c.claim(queued.job_id,'old-job')
        publish_standard(f,'old-job',claimed=job)
        cleanup=FakeCleanup(f.repo,f.clock); cleanup.set_protection('stream','old',license_clear=True)
        self.assertEqual(cleanup.plan('stream','old',STRICT_RETENTION).reason,'NOT_EXPIRED')
        self.assertEqual(f.repo.active('stream'),('old',1))

    def test_hold_during_cleanup_stops_next_batch(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        state=self.cleanup.start(plan,execute_fake=True)
        state=self.cleanup.step(plan,state,max_deletes=1)
        before=deepcopy(self.f.repo.datasets['old'])
        self.cleanup.set_protection('stream','old',hold=True)
        with self.assertRaises(ValueError): self.cleanup.step(plan,state,max_deletes=1)
        self.assertEqual(self.f.repo.datasets['old'],before)
        self.assertEqual(self.cleanup.storage['runs'][plan.plan_id].state,'BLOCKED')
        with self.assertRaises(ValueError): self.cleanup.set_protection('stream','old',shared=True)

    def test_publish_cleanup_race_has_no_dangling_pointer(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from app.jobs.contracts import Conflict
        for _ in range(10):
            self.setUp(); self.f.clock.advance(8*86400)
            self.f.policy=replace(self.f.policy,deadline=self.f.clock.now+timedelta(hours=3))
            new=self.f.job('retention-new')
            token=self.f.repo.claim_resource('stream',new.job_id,new.owner,new.fencing,new.revision)
            new,ok=self.f.ready(new,token,'new'); self.assertTrue(ok)
            plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
            gate=Barrier(2)
            def publish():
                gate.wait()
                try: return self.f.repo.publish('new','stream',new.job_id,new.owner,new.fencing,new.revision,token,'old',1)
                except Conflict: return 'CONFLICT'
            def cleanup():
                gate.wait()
                try: return self.cleanup.start(plan,execute_fake=True).state
                except ValueError: return 'STALE'
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(publish); b=pool.submit(cleanup); published,cleaned=a.result(),b.result()
            active,_=self.f.repo.active('stream')
            if published=='PUBLISHED':
                self.assertEqual((cleaned,active),('STALE','new'))
                self.assertEqual(self.f.repo.datasets['old']['state'],'SUPERSEDED')
            else:
                self.assertEqual((published,cleaned,active),('CONFLICT','READY',None))
                self.assertEqual(self.f.repo.datasets['old']['state'],'CLEANING')
            if active is not None: self.assertNotEqual(self.f.repo.datasets[active]['state'],'CLEANING')

    def test_publish_and_cleanup_both_deterministic_orders(self):
        from app.jobs.contracts import Conflict
        for first in ('publish','cleanup'):
            with self.subTest(first=first):
                self.setUp(); self.f.clock.advance(8*86400)
                self.f.policy=replace(self.f.policy,deadline=self.f.clock.now+timedelta(hours=3))
                new=self.f.job('deterministic-new')
                token=self.f.repo.claim_resource('stream',new.job_id,new.owner,new.fencing,new.revision)
                new,ok=self.f.ready(new,token,'new'); self.assertTrue(ok)
                plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
                def publish(): return self.f.repo.publish('new','stream',new.job_id,new.owner,new.fencing,new.revision,token,'old',1)
                if first=='publish':
                    self.assertEqual(publish(),'PUBLISHED')
                    with self.assertRaises(ValueError): self.cleanup.start(plan,execute_fake=True)
                    self.assertEqual(self.f.repo.active('stream')[0],'new')
                else:
                    self.cleanup.start(plan,execute_fake=True)
                    with self.assertRaises(Conflict): publish()
                    self.assertIsNone(self.f.repo.active('stream')[0])

    def test_license_raw_expiry_preserves_derived_and_supports_later_cleanup(self):
        from app.datasets.fake import digest
        self.f=test_refresh_offline.PublicationTests(); self.f.setUp()
        job,token=publish_standard(self.f,'raw-cleanup')
        self.cleanup=FakeCleanup(self.f.repo,self.f.clock)
        self.cleanup.set_protection('stream','old',license_clear=True)
        self.cleanup.set_protection('stream','old',license_raw_due=True)
        stored=self.f.repo.datasets['old']
        prior=deepcopy((stored['metadata'],stored['manifest'],stored['verified']))
        stored['metadata']['unknown_raw']='source-title'
        stored['manifest']['unknown_raw']='source-title'
        stored['verified']=digest({'manifest':stored['manifest'],'metadata':stored['metadata'],
                                   'counts':stored['verified_counts'],'quality_report':stored['quality_report']})
        with self.assertRaises(ValueError): self.cleanup.plan('stream','old',GRACE_FALLBACK)
        stored['metadata'],stored['manifest'],stored['verified']=prior
        plan=self.cleanup.plan('stream','old',GRACE_FALLBACK)
        self.assertEqual(plan.reason,'ELIGIBLE_LICENSE_RAW')
        self.assertNotIn('analysis_datasets/old',plan.paths)
        class Crash(BaseException): pass
        def crash(point):
            if point=='after_delete': raise Crash()
        crashing=FakeCleanup(self.f.repo,self.f.clock,fault_hook=crash)
        state=crashing.start(plan,execute_fake=True)
        with self.assertRaises(Crash): crashing.step(plan,state)
        saved_checkpoint=deepcopy(crashing.storage['runs'][plan.plan_id])
        self.assertEqual(saved_checkpoint.deletes,0)
        self.assertIn('scores-v1',saved_checkpoint.redacted_digests)
        import pickle,tempfile
        from pathlib import Path
        from app.jobs.fake import FakeCoordinator,FakeTransactionalStore
        from app.datasets.fake import FakeDatasetStore
        shared=self.f.c._store
        snapshot={k:getattr(shared,k) for k in ('jobs','keys','occurrences','resources','datasets','publications','retention_state')}
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'trusted-fake'; file.write_bytes(pickle.dumps(snapshot)); data=pickle.loads(file.read_bytes())
        restored=FakeTransactionalStore()
        for key,value in data.items(): setattr(restored,key,value)
        self.f.c=FakeCoordinator(self.f.clock,restored); self.f.repo=FakeDatasetStore(self.f.c)
        self.cleanup=FakeCleanup(self.f.repo,self.f.clock)
        state=self.cleanup.start(plan,execute_fake=True)
        self.assertEqual((state.cursor,state.reads,state.writes),(saved_checkpoint.cursor,saved_checkpoint.reads,saved_checkpoint.writes))
        state=self.cleanup.step(plan,state)
        self.assertEqual(state.state,'RETRY_WAIT')
        self.f.clock.advance(1)
        state=self.cleanup.step(plan,state)
        self.assertEqual(state.state,'PARTIALLY_DELETED')
        self.assertEqual(self.cleanup.pause(plan).state,'PARTIALLY_DELETED')
        with self.assertRaises(ValueError): self.cleanup.schedule_retry(plan,error_code='TRANSIENT_FAKE_ERROR')
        data=self.f.repo.datasets['old']
        self.assertEqual(data['state'],'PARTIALLY_DELETED')
        self.assertNotIn('raw-observations-v1',data['shards'])
        self.assertNotIn('snapshots-v1',data['shards'])
        self.assertIn('scores-v1',data['shards'])
        for row in data['shards']['scores-v1']['items']:
            self.assertNotIn('current_views_ceil',row)
            self.assertNotIn('baseline_observations',row)
            self.assertNotIn('article_original',row)
            self.assertLessEqual(set(row),{'keyword_id','score','status','algorithm_version'})
        self.assertEqual(self.f.repo.active('stream'),(None,2))
        self.assertEqual(self.f.repo.publish('old','stream',job.job_id,job.owner,job.fencing,job.revision,token,None,0),'ALREADY_PUBLISHED')
        self.assertEqual(self.f.repo.active('stream'),(None,2))
        self.cleanup.set_protection('stream','old',license_raw_due=False)
        self.f.clock.advance(8*86400)
        saved=deepcopy(data['shards']['scores-v1'])
        data['shards']['scores-v1']['items'][0]['current_views_ceil']=123
        with self.assertRaises(ValueError): self.cleanup.plan('stream','old',STRICT_RETENTION)
        data['shards']['scores-v1']=saved
        later=self.cleanup.plan('stream','old',STRICT_RETENTION)
        self.assertTrue(later.eligible)
        state=self.cleanup.start(later,execute_fake=True)
        for _ in range(20):
            if state.state=='COMPLETED': break
            state=self.cleanup.step(later,state,max_deletes=1)
        self.assertEqual(state.state,'COMPLETED')
        self.assertNotIn('old',self.f.repo.datasets)

    def test_license_raw_unknown_shard_reports_policy_conflict(self):
        f=test_refresh_offline.PublicationTests(); f.setUp()
        job=f.job('raw-extra'); token=f.repo.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
        job,ok=f.ready(job,token,'extra'); self.assertTrue(ok)
        f.repo.publish('extra','stream',job.job_id,job.owner,job.fencing,job.revision,token,None,0)
        cleanup=FakeCleanup(f.repo,f.clock); cleanup.set_protection('stream','extra',license_clear=True,license_raw_due=True)
        with self.assertRaises(ValueError): cleanup.plan('stream','extra',STRICT_RETENTION)
        self.assertEqual(f.repo.active('stream'),('extra',1))

    def test_unrecognized_manifest_shard_is_never_cleanup_target(self):
        f=test_refresh_offline.PublicationTests(); f.setUp()
        job=f.job('extra-shard'); token=f.repo.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
        job,ok=f.ready(job,token,'extra'); self.assertTrue(ok)
        f.repo.publish('extra','stream',job.job_id,job.owner,job.fencing,job.revision,token,None,0)
        cleanup=FakeCleanup(f.repo,f.clock); cleanup.set_protection('stream','extra',license_clear=True)
        f.clock.advance(8*86400)
        with self.assertRaises(ValueError): cleanup.plan('stream','extra',STRICT_RETENTION)
        self.assertIn('s1',f.repo.datasets['extra']['shards'])
        self.assertEqual(f.repo.active('stream'),('extra',1))

    def test_hold_injected_after_validation_cannot_unpoint_or_reserve(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        def hook(point):
            if point=='after_validation': self.cleanup.set_protection('stream','old',hold=True)
        cleanup=FakeCleanup(self.f.repo,self.f.clock,fault_hook=hook)
        with self.assertRaises(ValueError): cleanup.start(plan,execute_fake=True)
        self.assertEqual(self.f.repo.active('stream'),('old',1))
        self.assertEqual(self.f.repo.datasets['old']['state'],'ACTIVE')

    def test_missing_shard_during_cleanup_is_not_silently_accepted(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        checkpoint=self.cleanup.start(plan,execute_fake=True)
        name=next(iter(self.f.repo.datasets['old']['shards']))
        del self.f.repo.datasets['old']['shards'][name]
        with self.assertRaises(ValueError): self.cleanup.step(plan,checkpoint)
        self.assertEqual(self.cleanup.storage['runs'][plan.plan_id].deletes,0)

    def test_enumeration_is_dry_and_marks_invalid_dataset(self):
        self.f.clock.advance(8*86400)
        self.f.repo.datasets['bogus']={'resource':'stream'}
        before=deepcopy((self.f.repo.datasets,self.f.repo.resources,self.cleanup.storage))
        rows=self.cleanup.list_candidates(STRICT_RETENTION)
        self.assertEqual([row['reason'] for row in rows],['INVALID_DATASET','ELIGIBLE'])
        self.assertEqual((self.f.repo.datasets,self.f.repo.resources,self.cleanup.storage),before)

    def test_unrecognized_tenant_scope_cannot_enter_cleanup(self):
        self.f.clock.advance(8*86400)
        data=self.f.repo.datasets['old']
        self.f.c._jobs[data['job_id']].identity['tenant_scope']='tenant-private'
        with self.assertRaises(ValueError): self.cleanup.plan('stream','old',STRICT_RETENTION)
        self.assertEqual(self.f.repo.active('stream'),('old',1))

    def test_start_quota_includes_reservation_audit_and_pointer_write(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION,limits=Limits(writes=4))
        before=deepcopy((self.f.repo.datasets,self.f.repo.resources))
        state=self.cleanup.start(plan,execute_fake=True)
        self.assertEqual(state.state,'PAUSED')
        self.assertEqual((self.f.repo.datasets,self.f.repo.resources),before)

    def test_deadline_hit_mid_batch_stops_before_second_delete(self):
        self.f.clock.advance(8*86400); fired=[]
        def hook(point):
            if point=='after_delete' and not fired:
                fired.append(point); self.f.clock.advance(3601)
        cleanup=FakeCleanup(self.f.repo,self.f.clock,fault_hook=hook)
        plan=cleanup.plan('stream','old',STRICT_RETENTION)
        state=cleanup.start(plan,execute_fake=True)
        state=cleanup.step(plan,state,max_deletes=25)
        self.assertEqual((state.state,state.last_error,state.deletes),('FAILED','DEADLINE_EXPIRED',1))
        self.assertIn('old',self.f.repo.datasets)

    def test_parent_is_last_and_job_and_audit_are_preserved(self):
        self.f.clock.advance(8*86400)
        dataset=self.f.repo.datasets['old']; job_id=dataset['job_id']
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        self.assertEqual(plan.paths[-1],'analysis_datasets/old')
        self.assertEqual(set(plan.inventory_paths),set(plan.paths))
        self.assertEqual(plan.cleanup_manifest.document_paths,plan.inventory_paths)
        self.assertEqual(plan.cleanup_manifest.source_manifest_fingerprint,plan.manifest_fingerprint)
        state=self.cleanup.start(plan,execute_fake=True)
        for _ in range(len(plan.paths)-1): state=self.cleanup.step(plan,state,max_deletes=1)
        self.assertEqual(state.state,'DELETING')
        self.assertIn('old',self.f.repo.datasets)
        self.assertEqual(self.f.repo.datasets['old']['shards'],{})
        self.assertIsNone(self.f.repo.datasets['old']['quality_report'])
        state=self.cleanup.step(plan,state,max_deletes=1)
        self.assertEqual(state.state,'COMPLETED')
        self.assertIn(job_id,self.f.c._jobs)
        self.assertTrue(self.cleanup.storage['audit'])

    def test_unknown_subcollection_appearing_after_reservation_stops_delete(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        state=self.cleanup.start(plan,execute_fake=True)
        self.f.repo.datasets['old']['unknown_children']={'untouched':'sensitive'}
        with self.assertRaises(ValueError): self.cleanup.step(plan,state)
        self.assertEqual(self.f.repo.datasets['old']['unknown_children'],{'untouched':'sensitive'})
        self.assertEqual(self.cleanup.storage['runs'][plan.plan_id].deletes,0)

    def test_unknown_subcollection_injected_mid_batch_is_not_deleted(self):
        self.f.clock.advance(8*86400); fired=[]
        def hook(point):
            if point=='after_delete' and not fired:
                fired.append(point); self.f.repo.datasets['old']['unknown_children']={'untouched':True}
        cleanup=FakeCleanup(self.f.repo,self.f.clock,fault_hook=hook)
        plan=cleanup.plan('stream','old',STRICT_RETENTION)
        state=cleanup.start(plan,execute_fake=True)
        with self.assertRaises(ValueError): cleanup.step(plan,state,max_deletes=25)
        self.assertEqual(self.f.repo.datasets['old']['unknown_children'],{'untouched':True})
        self.assertEqual(cleanup.storage['runs'][plan.plan_id].deletes,1)

    def test_unknown_subcollection_injected_immediately_before_parent_is_preserved(self):
        self.f.clock.advance(8*86400)
        def hook(point):
            if point=='before_parent': self.f.repo.datasets['old']['unknown_children']={'untouched':True}
        cleanup=FakeCleanup(self.f.repo,self.f.clock,fault_hook=hook)
        plan=cleanup.plan('stream','old',STRICT_RETENTION)
        state=cleanup.start(plan,execute_fake=True)
        with self.assertRaises(ValueError): cleanup.step(plan,state,max_deletes=25)
        self.assertEqual(self.f.repo.datasets['old']['unknown_children'],{'untouched':True})
        self.assertNotEqual(cleanup.storage['runs'][plan.plan_id].state,'COMPLETED')

    def test_shared_references_from_multiple_tenants_are_independent(self):
        self.f.clock.advance(8*86400)
        self.cleanup.add_reference('stream','old','shared','tenant-a')
        self.cleanup.add_reference('stream','old','shared','tenant-b')
        self.cleanup.remove_reference('stream','old','shared','tenant-a')
        self.assertEqual(self.cleanup.plan('stream','old',STRICT_RETENTION).reason,'PROTECTED_REFERENCE')
        self.cleanup.remove_reference('stream','old','shared','tenant-b')
        self.assertTrue(self.cleanup.plan('stream','old',STRICT_RETENTION).eligible)

    def test_repeated_crash_before_batch_obeys_retry_budget(self):
        class Crash(BaseException): pass
        self.f.clock.advance(8*86400)
        def hook(point):
            if point=='before_batch': raise Crash()
        cleanup=FakeCleanup(self.f.repo,self.f.clock,fault_hook=hook)
        plan=cleanup.plan('stream','old',STRICT_RETENTION,limits=Limits(retries=1))
        state=cleanup.start(plan,execute_fake=True)
        with self.assertRaises(Crash): cleanup.step(plan,state,max_deletes=1)
        state=cleanup.step(plan,state,max_deletes=1)
        self.assertEqual((state.state,state.retries),('RETRY_WAIT',1))
        self.f.clock.advance(1)
        with self.assertRaises(Crash): cleanup.step(plan,state,max_deletes=1)
        state=cleanup.step(plan,state,max_deletes=1)
        self.assertEqual((state.state,state.last_error),('FAILED','RETRY_EXHAUSTED'))
        self.assertEqual(state.deletes,0)
        self.assertIn('old',self.f.repo.datasets)

    def test_replay_of_old_checkpoint_does_not_advance_another_batch(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        stale=self.cleanup.start(plan,execute_fake=True)
        first=self.cleanup.step(plan,stale,max_deletes=1)
        self.assertEqual(first.deletes,1)
        repeated=self.cleanup.step(plan,stale,max_deletes=1)
        self.assertEqual((repeated.cursor,repeated.deletes),(first.cursor,first.deletes))
        self.assertEqual(repeated.completed_paths,first.completed_paths)
        self.assertEqual(self.cleanup.step(plan,first,max_deletes=1).deletes,2)

    def test_two_cleanup_workers_with_same_cursor_delete_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        state=self.cleanup.start(plan,execute_fake=True); gate=Barrier(2)
        def run(_):
            gate.wait()
            return self.cleanup.step(plan,state,max_deletes=1)
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,range(2)))
        self.assertEqual([item.deletes for item in results],[1,1])
        self.assertEqual(self.cleanup.storage['runs'][plan.plan_id].deletes,1)

    def test_cancellation_stops_cleanup_without_reactivating_partial_data(self):
        self.f.clock.advance(8*86400)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        state=self.cleanup.start(plan,execute_fake=True)
        state=self.cleanup.step(plan,state,max_deletes=1)
        state=self.cleanup.cancel(plan)
        self.assertEqual((state.state,state.last_error),('PAUSED','CANCELLED_BY_OPERATOR'))
        self.assertEqual(self.cleanup.step(plan,state).deletes,1)
        self.assertEqual(self.f.repo.datasets['old']['state'],'CLEANING')
        self.assertIsNone(self.f.repo.active('stream')[0])

    def test_missing_or_tampered_manifest_and_partial_draft_fail_closed(self):
        self.f.clock.advance(8*86400)
        d=self.f.repo.datasets['old']; saved=deepcopy(d['manifest'])
        d['manifest']=None
        with self.assertRaises(ValueError): self.cleanup.plan('stream','old',STRICT_RETENTION)
        d['manifest']=deepcopy(saved); d['manifest']['shards']['scores-v1']['sha256']='forged'
        with self.assertRaises(ValueError): self.cleanup.plan('stream','old',STRICT_RETENTION)
        d['manifest']=saved
        self.f.repo.datasets['empty']={'resource':'stream','state':'DRAFT','shards':{},'manifest':None}
        with self.assertRaises(ValueError): self.cleanup.plan('stream','empty',STRICT_RETENTION)
        self.assertEqual(self.f.repo.datasets['empty']['state'],'DRAFT')
        self.assertEqual(self.f.repo.active('stream'),('old',1))

    def test_verified_empty_dataset_has_explicit_expired_policy(self):
        from app.jobs.contracts import State
        from app.datasets.fake import digest
        from app.datasets.manifest import build_offline_manifest
        from refresh_fixtures import strict_output
        f=test_refresh_offline.PublicationTests(); f.setUp()
        job=f.job('empty-verified'); token=f.repo.claim_resource('stream',job.job_id,job.owner,job.fencing,job.revision)
        for target in (State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING): job=f.state(job,target)
        output=strict_output({'shards':{},'quality_ok':True})
        snap=output['shards']['snapshots-v1']['items'][0]; snap['item_count']=0
        output['shards']['raw-observations-v1']['items'][0]['rows']=[]
        output['shards']['scores-v1']['items']=[]
        report=output['quality_report']; report['keyword_count']=0; report['observed_count']=2
        report['snapshots'][0].update(raw_count=0,accepted_count=0,rejected_count=0,duplicate_count=0,rejection_reasons={},quality_flags=[])
        metadata=dict(output['manifest_input'],occurrence_id=job.identity['occurrence_id']); report=dict(report,dataset_id='empty')
        f.repo.draft('empty','stream',job.job_id,job.owner,job.fencing,job.revision,token,metadata)
        for key,value in output['shards'].items(): f.repo.put_shard('empty','stream',job.job_id,job.owner,job.fencing,job.revision,token,key,value)
        f.repo.put_quality_report('empty','stream',job.job_id,job.owner,job.fencing,job.revision,token,report)
        manifest=build_offline_manifest(dataset_id='empty',job_id=job.job_id,resource_id='stream',identity=job.identity,metadata=metadata,shards=output['shards'],quality_report=report,hash_fn=digest)
        f.repo.seal('empty','stream',job.job_id,job.owner,job.fencing,job.revision,token,manifest)
        job=f.state(job,State.VALIDATING)
        self.assertTrue(f.repo.verify('empty','stream',job.job_id,job.owner,job.fencing,job.revision,token,quality_ok=None))
        f.repo.publish('empty','stream',job.job_id,job.owner,job.fencing,job.revision,token,None,0)
        cleanup=FakeCleanup(f.repo,f.clock); cleanup.set_protection('stream','empty',license_clear=True)
        self.assertEqual(cleanup.plan('stream','empty',STRICT_RETENTION).reason,'NOT_EXPIRED')
        f.clock.advance(8*86400)
        plan=cleanup.plan('stream','empty',STRICT_RETENTION)
        self.assertEqual(plan.reason,'ELIGIBLE_EMPTY')
        state=cleanup.start(plan,execute_fake=True)
        for _ in range(20):
            if state.state=='COMPLETED': break
            state=cleanup.step(plan,state,max_deletes=1)
        self.assertEqual(state.state,'COMPLETED')

    def test_old_worker_cannot_modify_reserved_dataset(self):
        from app.jobs.contracts import LeaseLost
        self.f.clock.advance(8*86400)
        d=self.f.repo.datasets['old']; job=self.f.c._jobs[d['job_id']]
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION)
        self.cleanup.start(plan,execute_fake=True)
        with self.assertRaises(LeaseLost):
            self.f.repo.put_shard('old','stream',job.job_id,job.owner,job.fencing,job.revision,d['token'],'bad',{'items':[]})
        self.assertNotIn('bad',d['shards'])

    def test_legacy_demo_and_other_namespaces_are_not_cleanup_targets(self):
        from app.datasets.fake import FakeDatasetStore
        from app.retention.offline import DRY_RUN,LIVE_DELETE_ENABLED
        self.assertTrue(DRY_RUN)
        self.assertFalse(LIVE_DELETE_ENABLED)
        with self.assertRaises(ValueError): FakeCleanup(object(),self.f.clock)
        self.f.clock.advance(8*86400)
        sentinel={'users':{'settings':'keep'},'secrets':{'key':'keep'},'saved_research':{'item':'keep'}}
        self.f.c._store.non_dataset_data=deepcopy(sentinel)
        for dataset in ('30e96cf96abe48dbb304fb9808c89873','users','secrets','saved_research'):
            with self.assertRaises(ValueError): self.cleanup.plan('stream',dataset,STRICT_RETENTION)
        plan=self.cleanup.plan('stream','old',STRICT_RETENTION); state=self.cleanup.start(plan,execute_fake=True)
        for _ in range(20):
            if state.state=='COMPLETED': break
            state=self.cleanup.step(plan,state,max_deletes=1)
        self.assertEqual(state.state,'COMPLETED')
        self.assertEqual(self.f.c._store.non_dataset_data,sentinel)

    def test_budget_cutoffs_never_exceed_limits_or_complete_early(self):
        for dimension,values in (('reads',range(1,90,3)),('writes',range(1,50,2)),('deletes',range(1,8))):
            for limit in values:
                with self.subTest(dimension=dimension,limit=limit):
                    self.setUp(); self.f.clock.advance(8*86400)
                    kwargs={dimension:limit}
                    plan=self.cleanup.plan('stream','old',STRICT_RETENTION,limits=Limits(**kwargs))
                    state=self.cleanup.start(plan,execute_fake=True)
                    for _ in range(30):
                        if state.state in ('COMPLETED','PAUSED','FAILED'): break
                        state=self.cleanup.step(plan,state,max_deletes=1)
                    self.assertLessEqual(state.reads,plan.max_reads)
                    self.assertLessEqual(state.writes,plan.max_writes)
                    self.assertLessEqual(state.deletes,plan.max_deletes)
                    if state.state=='COMPLETED':
                        self.assertEqual(state.completed_paths,list(plan.paths))
                        self.assertNotIn('old',self.f.repo.datasets)
                    else:
                        self.assertEqual(state.state,'PAUSED')
                        self.assertIn('old',self.f.repo.datasets)
                    active,_=self.f.repo.active('stream')
                    if active is not None: self.assertEqual(self.f.repo.datasets[active]['state'],'ACTIVE')
