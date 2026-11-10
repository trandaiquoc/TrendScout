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
        with self.jobs._mutex:
            stored=self.jobs._jobs[job_id]
            if stored.refresh_budget is None:
                from copy import deepcopy
                stored.refresh_budget=deepcopy(self.budget)
            elif (stored.refresh_budget.max_reads, stored.refresh_budget.max_writes) != (self.budget.max_reads, self.budget.max_writes):
                raise ValueError("job budget cannot be changed")
        def charge(*, reads=0, writes=0):
            with self.jobs._mutex:
                self.jobs._jobs[job_id].refresh_budget.charge(reads=reads,writes=writes)
        charge(reads=1,writes=1)  # estimated job claim
        job=self.jobs.claim(job_id,owner)
        self.checkpoint_hook("after_claim")
        charge(reads=1,writes=1)  # estimated resource claim
        token=self.datasets.claim_resource(resource,job_id,owner,job.fencing,job.revision)
        if fresh_on_retry:
            dataset_id = f"{dataset_id}-f{token}"
        def checkpoint(requests=0, name="checkpoint"):
            nonlocal job
            self.checkpoint_hook(name)
            charge(reads=2,writes=1)  # job read and checkpoint CAS
            latest=self.jobs.get(job_id)
            job=self.jobs.checkpoint(job_id,owner,job.fencing,latest.revision,requests=requests)
            if job.state == State.CANCELLING:
                job=transition(job_id,owner,job.fencing,job.revision,State.CANCELLED)
                raise Cancelled()
            charge(reads=2,writes=2)  # job and resource heartbeat
            job=self.datasets.heartbeat_resource(resource,job_id,owner,job.fencing,job.revision,token)
        def transition(*args,**kwargs):
            charge(reads=1,writes=1)  # estimated job transition
            return self.jobs.transition(*args,**kwargs)
        try:
            pipeline=getattr(self.fetch,'__self__',None)
            bind=getattr(pipeline,'set_request_guard',None)
            if callable(bind): bind(lambda: checkpoint(1, "before_source_request"))
            checkpoint(0 if callable(bind) else 1, "before_fetch")
            job=transition(job_id,owner,job.fencing,job.revision,State.FETCHING)
            raw=self.fetch()
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
            charge(writes=1)
            self.datasets.draft(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,strict_metadata)
            checkpoint(name="after_draft")
            for key, value in output['shards'].items():
                checkpoint(name="before_shard")
                charge(writes=1)
                self.datasets.put_shard(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,key,value)
                self.checkpoint_hook('after_shard')
            checkpoint(name="before_seal")
            if not isinstance(output.get('manifest_input'),dict) or not isinstance(output.get('quality_report'),dict): raise ValueError('strict manifest inputs required')
            report=dict(output['quality_report'],dataset_id=dataset_id)
            charge(writes=1)
            self.datasets.put_quality_report(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,report)
            metadata=dict(output['manifest_input'],occurrence_id=job.identity['occurrence_id'])
            manifest=build_offline_manifest(dataset_id=dataset_id,job_id=job_id,resource_id=resource,identity=job.identity,
                                            metadata=metadata,shards=output['shards'],quality_report=report,hash_fn=digest)
            charge(writes=1)
            self.datasets.seal(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,manifest)
            checkpoint(name="after_seal")
            job=transition(job_id,owner,job.fencing,job.revision,State.VALIDATING)
            checkpoint(name="before_verify")
            charge(reads=len(output["shards"]), writes=1)
            if not self.datasets.verify(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,quality_ok=None):
                raise ValueError('quality or manifest verification failed')
            checkpoint(name="before_publish")
            charge(reads=3, writes=3)
            result = self.datasets.publish(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,expected_active,expected_revision)
            self.checkpoint_hook("after_publish")
            return result
        except Cancelled:
            return 'CANCELLED'
        except (LeaseLost, Conflict):
            raise
        except RefreshError as error:
            current=self.jobs.get(job_id)
            delay = self.retry_policy.delay(error,current.attempts,self.jobs._now(),current.policy.deadline) if self.retry_policy else None
            # The fake coordinator controls retry counts and backoff; no sleeping here.
            # Retry-After is honored only if its existing configured delay is long enough.
            if (delay is not None and current.retries_used < current.policy.retry_budget and current.attempts < current.policy.max_attempts):
                charge(reads=2,writes=2)  # estimated retry state and resource release
                self.datasets.retry_wait(resource,job_id,owner,job.fencing,current.revision,token,error.code.value,delay)
            else:
                transition(job_id,owner,job.fencing,current.revision,State.FAILED,reason_code=error.code.value)
            raise
        except Exception:
            # Never expose external error text; stale owner cannot mutate failure state.
            current=self.jobs.get(job_id)
            if current.state not in (State.COMPLETED,State.CANCELLED):
                transition(job_id,owner,job.fencing,current.revision,State.FAILED,reason_code='REFRESH_FAILED')
            raise
