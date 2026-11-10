"""Offline worker prototype with injected steps; never starts itself or calls live sources."""
from app.jobs.contracts import State, LeaseLost, Conflict
from app.datasets.fake import digest


class Cancelled(Exception): pass


class RefreshWorker:
    def __init__(self, coordinator, datasets, fetch, clean, analyze):
        self.jobs=coordinator; self.datasets=datasets
        self.fetch=fetch; self.clean=clean; self.analyze=analyze

    def run(self, job_id, owner, resource, dataset_id, *, expected_active, expected_revision):
        job=self.jobs.claim(job_id,owner)
        token=self.datasets.claim_resource(resource,job_id,owner,job.fencing,job.revision)
        def checkpoint(requests=0):
            nonlocal job
            latest=self.jobs.get(job_id)
            job=self.jobs.checkpoint(job_id,owner,job.fencing,latest.revision,requests=requests)
            if job.state == State.CANCELLING:
                job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.CANCELLED)
                raise Cancelled()
        try:
            checkpoint(1)
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.FETCHING)
            raw=self.fetch()
            checkpoint()
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.NORMALIZING)
            cleaned=self.clean(raw)
            checkpoint()
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.ANALYZING)
            output=self.analyze(cleaned)
            checkpoint()
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.PERSISTING)
            self.datasets.draft(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,output['metadata'])
            for key, value in output['shards'].items():
                checkpoint()
                self.datasets.put_shard(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,key,value)
            manifest={key:digest(value) for key,value in output['shards'].items()}
            self.datasets.seal(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,manifest)
            job=self.jobs.transition(job_id,owner,job.fencing,job.revision,State.VALIDATING)
            checkpoint()
            if not self.datasets.verify(dataset_id,resource,job_id,owner,job.fencing,job.revision,token,quality_ok=output['quality_ok']):
                raise ValueError('quality or manifest verification failed')
            checkpoint()
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
