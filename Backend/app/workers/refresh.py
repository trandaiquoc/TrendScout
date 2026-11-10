"""Offline worker prototype with injected steps; never starts itself or calls live sources."""
from app.jobs.contracts import State, LeaseLost, Conflict
from app.datasets.fake import digest
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
        job=self.jobs.claim(job_id,owner)
        self.checkpoint_hook("after_claim")
        token=self.datasets.claim_resource(resource,job_id,owner,job.fencing,job.revision)
        if fresh_on_retry:
            dataset_id = f"{dataset_id}-f{token}"
        def checkpoint(requests=0, name="checkpoint"):
            nonlocal job
            self.checkpoint_hook(name)
            latest=self.jobs.get(job_id)
            job=self.jobs.checkpoint(job_id,owner,job.fencing,latest.revision,requests=requests)
            if job.state == State.CANCELLING:
                job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.CANCELLED)
                raise Cancelled()
            job=self.datasets.heartbeat_resource(resource,job_id,owner,job.fencing,job.revision,token)
        def charge(*, reads=0, writes=0):
            with self.jobs._mutex:
                self.jobs._jobs[job_id].refresh_budget.charge(reads=reads,writes=writes)
        try:
            checkpoint(1, "before_fetch")
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.FETCHING)
            raw=self.fetch()
            checkpoint(name="after_fetch")
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.NORMALIZING)
            checkpoint(name="before_clean")
            cleaned=self.clean(raw)
            checkpoint(name="after_clean")
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.ANALYZING)
            checkpoint(name="before_analysis")
            output=self.analyze(cleaned)
            checkpoint(name="after_analysis")
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.PERSISTING)
            checkpoint(name="before_draft")
            charge(writes=1)
            self.datasets.draft(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,output['metadata'])
            checkpoint(name="after_draft")
            for key, value in output['shards'].items():
                checkpoint(name="before_shard")
                charge(writes=1)
                self.datasets.put_shard(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,key,value)
                self.checkpoint_hook('after_shard')
            checkpoint(name="before_seal")
            manifest=output.get('manifest') or {key:digest(value) for key,value in output['shards'].items()}
            charge(writes=1)
            self.datasets.seal(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,manifest)
            checkpoint(name="after_seal")
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.VALIDATING)
            checkpoint(name="before_verify")
            charge(reads=len(output["shards"]), writes=1)
            if not self.datasets.verify(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,quality_ok=output['quality_ok']):
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
                self.datasets.retry_wait(resource,job_id,owner,job.fencing,current.revision,token,error.code.value,delay)
            else:
                self.jobs.transition(job_id,owner,job.fencing,current.revision,State.FAILED,reason_code=error.code.value)
            raise
        except Exception:
            # Never expose external error text; stale owner cannot mutate failure state.
            current=self.jobs.get(job_id)
            if current.state not in (State.COMPLETED,State.CANCELLED):
                self.jobs.transition(job_id,owner,job.fencing,current.revision,State.FAILED,reason_code='REFRESH_FAILED')
            raise
