"""Offline worker prototype with injected steps; never starts itself or calls live sources."""
from app.jobs.contracts import State, LeaseLost, Conflict
from app.datasets.fake import digest


class Cancelled(Exception): pass


class RefreshWorker:
    def __init__(self, coordinator, datasets, fetch, clean, analyze, *, checkpoint_hook=None):
        self.jobs=coordinator; self.datasets=datasets
        self.fetch=fetch; self.clean=clean; self.analyze=analyze
        self.checkpoint_hook=checkpoint_hook or (lambda name: None)

    def run(self, job_id, owner, resource, dataset_id, *, expected_active, expected_revision, fresh_on_retry=False):
        job=self.jobs.claim(job_id,owner)
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
            self.datasets.draft(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,output['metadata'])
            checkpoint(name="after_draft")
            for key, value in output['shards'].items():
                checkpoint(name="before_shard")
                self.datasets.put_shard(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,key,value)
            checkpoint(name="before_seal")
            manifest={key:digest(value) for key,value in output['shards'].items()}
            self.datasets.seal(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,manifest)
            checkpoint(name="after_seal")
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.VALIDATING)
            checkpoint(name="before_verify")
            if not self.datasets.verify(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,quality_ok=output['quality_ok']):
                raise ValueError('quality or manifest verification failed')
            checkpoint(name="before_publish")
            return self.datasets.publish(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,expected_active,expected_revision)
        except Cancelled:
            return 'CANCELLED'
        except (LeaseLost, Conflict):
            raise
        except Exception:
            # Never expose external error text; stale owner cannot mutate failure state.
            current=self.jobs.get(job_id)
            if current.state not in (State.COMPLETED,State.CANCELLED):
                self.jobs.transition(job_id,owner,job.fencing,current.revision,State.FAILED,reason_code='REFRESH_FAILED')
            raise
