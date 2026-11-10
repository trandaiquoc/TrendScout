"""Fake publication transaction: all state under the same in-process lock as jobs."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json

from app.jobs.contracts import State, Conflict, LeaseLost, InvalidTransition


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class FakeDatasetStore:
    """NOT durable, and not a Firestore transaction or production locking solution."""
    def __init__(self, coordinator):
        self.c = coordinator
        self.lock = coordinator._mutex
        self.resources = {}
        self.datasets = {}
        self.publications = {}

    def claim_resource(self, resource, job_id, owner, job_token, revision):
        with self.lock:
            now=self.c._now(); job=self.c._jobs[job_id]; self.c._owned(job,owner,job_token,revision,now)
            if job.state not in (State.QUEUED,State.FETCHING,State.NORMALIZING,State.ANALYZING,State.PERSISTING,State.VALIDATING): raise InvalidTransition('job cannot claim')
            old=self.resources.get(resource)
            if old and old['owner'] is not None and old['expires_at'] > now: raise Conflict('resource leased')
            token=(old['token'] if old else 0)+1
            self.resources[resource]={'job_id':job_id,'owner':owner,'token':token,'expires_at':job.expires_at,
                                      'active':old['active'] if old else None,'pointer_revision':old['pointer_revision'] if old else 0}
            return token

    def _owned(self, resource, job_id, owner, job_token, revision, resource_token):
        now=self.c._now(); job=self.c._jobs[job_id]; self.c._owned(job,owner,job_token,revision,now)
        lease=self.resources[resource]
        if lease['job_id'] != job_id or lease['owner'] != owner or lease['token'] != resource_token or lease['expires_at'] <= now:
            raise LeaseLost('resource lease lost')
        return job, lease

    def heartbeat_resource(self, resource, job_id, owner, job_token, revision, token):
        """Renew job and resource together in the fake transaction boundary."""
        with self.lock:
            job, lease = self._owned(resource, job_id, owner, job_token, revision, token)
            updated = self.c.heartbeat(job_id, owner, job_token, revision)
            lease['expires_at'] = updated.expires_at
            return updated

    def draft(self, dataset_id, resource, job_id, owner, job_token, revision, token, metadata):
        with self.lock:
            self._owned(resource,job_id,owner,job_token,revision,token)
            if dataset_id in self.datasets:
                d=self.datasets[dataset_id]
                if (d['job_id'],d['token'],d['metadata']) != (job_id,token,metadata): raise Conflict('draft identity conflict')
                return
            self.datasets[dataset_id]={'resource':resource,'job_id':job_id,'owner':owner,'token':token,'metadata':deepcopy(metadata),
                                       'state':'DRAFT','shards':{},'manifest':None,'verified':None}

    def put_shard(self, dataset_id, resource, job_id, owner, job_token, revision, token, shard_id, data):
        with self.lock:
            self._owned(resource,job_id,owner,job_token,revision,token)
            d=self.datasets[dataset_id]
            if (d['resource'],d['job_id'],d['owner'],d['token'],d['state']) != (resource,job_id,owner,token,'DRAFT'): raise Conflict('immutable or foreign draft')
            if shard_id in d['shards'] and d['shards'][shard_id] != data: raise Conflict('immutable shard')
            d['shards'][shard_id]=deepcopy(data)

    def seal(self, dataset_id, resource, job_id, owner, job_token, revision, token, expected):
        with self.lock:
            self._owned(resource,job_id,owner,job_token,revision,token)
            d=self.datasets[dataset_id]
            if (d['resource'],d['job_id'],d['owner'],d['token'],d['state']) != (resource,job_id,owner,token,'DRAFT'): raise Conflict('cannot seal')
            if not isinstance(expected,dict) or not expected: raise ValueError('empty manifest')
            d['manifest']=deepcopy(expected); d['state']='VALIDATING'

    def verify(self, dataset_id, resource, job_id, owner, job_token, revision, token, *, quality_ok):
        with self.lock:
            self._owned(resource,job_id,owner,job_token,revision,token)
            d=self.datasets[dataset_id]
            if (d['resource'],d['job_id'],d['owner'],d['token'],d['state']) != (resource,job_id,owner,token,'VALIDATING'): raise Conflict('wrong draft')
            actual={key:digest(value) for key,value in d['shards'].items()}
            # Derive a minimal quality gate from persisted content, not just a caller flag.
            valid_content = bool(d['metadata'].get('source')) and all(isinstance(v, dict) and isinstance(v.get('items'), list) for v in d['shards'].values())
            item_count = sum(len(v['items']) for v in d['shards'].values()) if valid_content else 0
            if quality_ok is not True or actual != d['manifest'] or item_count == 0:
                d['state']='REJECTED'; return False
            d['verified_counts']={'shards':len(actual),'items':item_count}
            d['verified']=digest({'manifest':d['manifest'],'metadata':d['metadata'],'counts':d['verified_counts']})
            d['state']='READY'; return True

    def read_dataset(self, dataset_id):
        with self.lock:
            return deepcopy(self.datasets.get(dataset_id))

    def active(self,resource):
        with self.lock:
            r=self.resources.get(resource)
            return (r['active'],r['pointer_revision']) if r else (None,0)

    def publish(self, dataset_id, resource, job_id, owner, job_token, revision, token, expected_active, expected_revision):
        with self.lock:
            # A retry after a committed response lost can observe its own publication, never roll back a newer pointer.
            prior=self.publications.get((resource,dataset_id))
            if prior is not None:
                if prior != (job_id,owner,token): raise Conflict('publication identity mismatch')
                return 'ALREADY_PUBLISHED'
            job, lease=self._owned(resource,job_id,owner,job_token,revision,token)
            d=self.datasets[dataset_id]
            if job.state != State.VALIDATING or d['state'] != 'READY' or (d['resource'],d['job_id'],d['owner'],d['token']) != (resource,job_id,owner,token): raise Conflict('not ready')
            if d['verified'] != digest({'manifest':d['manifest'],'metadata':d['metadata'],'counts':d['verified_counts']}): raise Conflict('manifest changed')
            if (lease['active'],lease['pointer_revision']) != (expected_active,expected_revision): raise Conflict('pointer CAS')
            if expected_active is not None: self.datasets[expected_active]['state']='SUPERSEDED'
            lease['active']=dataset_id; lease['pointer_revision']+=1
            d['state']='ACTIVE'; job.state=State.COMPLETED; job.reason_code=None
            self.c._record(job,self.c._now(),'PUBLISHED')
            self.publications[resource,dataset_id]=(job_id,owner,token)
            return 'PUBLISHED'
