"""Offline worker prototype with injected steps; never starts itself or calls live sources."""
from app.jobs.contracts import State, LeaseLost, Conflict
from app.datasets.fake import digest
from app.datasets.manifest import build_offline_manifest
from app.workers.safety import OfflineBudget, RefreshError, RetryPolicy


class Cancelled(Exception): pass


class RefreshWorker:
    def __init__(self, coordinator, datasets, fetch, clean, analyze, *, checkpoint_hook=None, budget=None, retry_policy=None):
        self.jobs=coordinator; self.datasets=datasets
        self.fetch=fetch; self.clean=clean; self.analyze=analyze
        self.checkpoint_hook=checkpoint_hook or (lambda name: None)
        self.budget = budget
        self.retry_policy = retry_policy
        if not isinstance(budget, OfflineBudget): raise ValueError("offline refresh budget required")
        if retry_policy is not None and not isinstance(retry_policy, RetryPolicy): raise ValueError("invalid retry policy")

    def run(self, job_id, owner, resource, dataset_id, *, expected_active, expected_revision, fresh_on_retry=False):
        from app.workers.offline_pipeline import OfflineWikimediaPipeline
        source=getattr(self.fetch,'__self__',None)
        if type(source) is not OfflineWikimediaPipeline or getattr(self.fetch,'__func__',None) is not OfflineWikimediaPipeline.fetch:
            raise ValueError('guarded offline Wikimedia fetch required')
        with self.jobs._mutex:
            stored=self.jobs._jobs[job_id]
            if not isinstance(stored.refresh_budget, OfflineBudget):
                raise ValueError('job operation budget not configured')
            if (stored.refresh_budget.max_reads, stored.refresh_budget.max_writes) != (self.budget.max_reads, self.budget.max_writes):
                raise ValueError("job budget cannot be changed")
        def charge(*, reads=0, writes=0):
            with self.jobs._mutex:
                self.jobs._jobs[job_id].refresh_budget.charge(reads=reads,writes=writes)
        if stored.state == State.COMPLETED:
            return self.datasets.replay_publication(resource,job_id,owner,dataset_id,fresh_on_retry or stored.attempts > 1,expected_active,expected_revision)
        job=self.jobs.claim(job_id,owner)
        self.checkpoint_hook("after_claim")
        token=self.datasets.claim_resource(resource,job_id,owner,job.fencing,job.revision)
        if fresh_on_retry or job.attempts > 1:
            dataset_id = f"{dataset_id}-f{token}"
        def checkpoint(requests=0, name="checkpoint"):
            nonlocal job
            self.checkpoint_hook(name)
            latest=self.jobs.get_owned(job_id,owner,job.fencing)
            job=self.jobs.checkpoint(job_id,owner,job.fencing,latest.revision,requests=requests)
            if job.state == State.CANCELLING:
                job=transition(job_id,owner,job.fencing,job.revision,State.CANCELLED)
                raise Cancelled()
            job=self.datasets.heartbeat_resource(resource,job_id,owner,job.fencing,job.revision,token)
        def transition(*args,**kwargs):
            return self.jobs.transition(*args,**kwargs)
        try:
            pipeline=getattr(self.fetch,'__self__',None)
            bind=getattr(pipeline,'set_request_guard',None)
            checkpoint(0 if callable(bind) else 1, "before_fetch")
            job=transition(job_id,owner,job.fencing,job.revision,State.FETCHING)
            guard_token=bind(lambda: checkpoint(1, "before_source_request"))
            try:
                raw=self.fetch()
            finally:
                pipeline.reset_request_guard(guard_token)
            checkpoint(name="after_fetch")
            job=transition(job_id,owner,job.fencing,job.revision,State.NORMALIZING)
            checkpoint(name="before_clean")
            cleaned=self.clean(raw)
            checkpoint(name="after_clean")
            job=transition(job_id,owner,job.fencing,job.revision,State.ANALYZING)
            checkpoint(name="before_analysis")
            output=self.analyze(cleaned)
            checkpoint(name="after_analysis")
            job=transition(job_id,owner,job.fencing,job.revision,State.PERSISTING)
            checkpoint(name="before_draft")
            if not isinstance(output.get('manifest_input'),dict): raise ValueError('strict manifest inputs required')
            strict_metadata=dict(output['manifest_input'],occurrence_id=job.identity['occurrence_id'])
            self.datasets.draft(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,strict_metadata)
            checkpoint(name="after_draft")
            for key, value in output['shards'].items():
                checkpoint(name="before_shard")
                self.datasets.put_shard(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,key,value)
                self.checkpoint_hook('after_shard')
            checkpoint(name="before_seal")
            if not isinstance(output.get('manifest_input'),dict) or not isinstance(output.get('quality_report'),dict): raise ValueError('strict manifest inputs required')
            report=dict(output['quality_report'],dataset_id=dataset_id)
            self.datasets.put_quality_report(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,report)
            metadata=dict(output['manifest_input'],occurrence_id=job.identity['occurrence_id'])
            manifest=build_offline_manifest(dataset_id=dataset_id,job_id=job_id,resource_id=resource,identity=job.identity,
                                            metadata=metadata,shards=output['shards'],quality_report=report,hash_fn=digest)
            self.datasets.seal(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,manifest)
            checkpoint(name="after_seal")
            job=transition(job_id,owner,job.fencing,job.revision,State.VALIDATING)
            checkpoint(name="before_verify")
            if not self.datasets.verify(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,quality_ok=None):
                raise ValueError('quality or manifest verification failed')
            checkpoint(name="before_publish")
            result = self.datasets.publish(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,expected_active,expected_revision)
            self.checkpoint_hook("after_publish")
            return result
        except Cancelled:
            return 'CANCELLED'
        except (LeaseLost, Conflict):
            raise
        except RefreshError as error:
            current=self.jobs.get_owned(job_id,owner,job.fencing)
            delay = self.retry_policy.delay(error,current.attempts,self.jobs._now(),current.policy.deadline) if self.retry_policy else None
            # The fake coordinator controls retry counts and backoff; no sleeping here.
            # Retry-After is honored only if its existing configured delay is long enough.
            if (delay is not None and current.retries_used < current.policy.retry_budget and current.attempts < current.policy.max_attempts):
                self.datasets.retry_wait(resource,job_id,owner,job.fencing,current.revision,token,error.code.value,delay)
            else:
                transition(job_id,owner,job.fencing,current.revision,State.FAILED,reason_code=error.code.value)
            raise
        except Exception:
            # Never expose external error text; stale owner cannot mutate failure state.
            current=self.jobs.get_owned(job_id,owner,job.fencing)
            if current.state not in (State.COMPLETED,State.CANCELLED):
                transition(job_id,owner,job.fencing,current.revision,State.FAILED,reason_code='REFRESH_FAILED')
            raise
