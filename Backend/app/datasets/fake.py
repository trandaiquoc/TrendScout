"""Fake publication transaction: all state under the same in-process lock as jobs."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json

from app.jobs.contracts import State, Conflict, LeaseLost, InvalidTransition
from app.datasets.manifest import validate_manifest
from app.datasets.evidence import validate_source_evidence


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class FakeDatasetStore:
    """NOT durable, and not a Firestore transaction or production locking solution."""
    def __init__(self, coordinator):
        self.c = coordinator
        self.lock = coordinator._mutex
        self.resources = coordinator._store.resources
        self.datasets = coordinator._store.datasets
        self.publications = coordinator._store.publications

    def claim_resource(self, resource, job_id, owner, job_token, revision):
        with self.lock:
            now=self.c._now(); job=self.c._jobs[job_id]; self.c._owned(job,owner,job_token,revision,now); self.c._charge(job,reads=1,writes=1)
            if job.identity.get('resource_id') != resource: raise Conflict('resource identity mismatch')
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

    def retry_wait(self, resource, job_id, owner, job_token, revision, token, reason, delay=None):
        """Release the fake resource and enqueue a bounded retry atomically."""
        with self.lock:
            job, lease = self._owned(resource, job_id, owner, job_token, revision, token)
            self.c._charge(self.c._jobs[job_id],reads=1,writes=1,kind="retry")
            if delay is not None and (not isinstance(delay, timedelta) or delay.total_seconds() < 0): raise ValueError('invalid retry delay')
            updated = self.c.transition(job_id, owner, job_token, revision, State.RETRY_WAIT, reason_code=reason)
            if delay is not None:
                job.retry_after=self.c._now()+delay
                updated=deepcopy(job)
            lease['owner']=None; lease['expires_at']=self.c._now()
            return updated

    def heartbeat_resource(self, resource, job_id, owner, job_token, revision, token):
        """Renew job and resource together in the fake transaction boundary."""
        with self.lock:
            job, lease = self._owned(resource, job_id, owner, job_token, revision, token)
            self.c._charge(self.c._jobs[job_id],reads=1,writes=1,kind="heartbeat")
            updated = self.c.heartbeat(job_id, owner, job_token, revision)
            lease['expires_at'] = updated.expires_at
            return updated

    def draft(self, dataset_id, resource, job_id, owner, job_token, revision, token, metadata):
        with self.lock:
            self._owned(resource,job_id,owner,job_token,revision,token)
            self.c._charge(self.c._jobs[job_id],reads=1,writes=1)
            if dataset_id in self.datasets:
                d=self.datasets[dataset_id]
                if (d['job_id'],d['token'],d['metadata']) != (job_id,token,metadata): raise Conflict('draft identity conflict')
                return
            self.datasets[dataset_id]={'resource':resource,'job_id':job_id,'owner':owner,'token':token,'metadata':deepcopy(metadata),
                                       'state':'DRAFT','shards':{},'quality_report':None,'manifest':None,'verified':None}

    def put_shard(self, dataset_id, resource, job_id, owner, job_token, revision, token, shard_id, data):
        with self.lock:
            self._owned(resource,job_id,owner,job_token,revision,token)
            self.c._charge(self.c._jobs[job_id],reads=1,writes=1)
            d=self.datasets[dataset_id]
            if (d['resource'],d['job_id'],d['owner'],d['token'],d['state']) != (resource,job_id,owner,token,'DRAFT'): raise Conflict('immutable or foreign draft')
            if shard_id in d['shards'] and d['shards'][shard_id] != data: raise Conflict('immutable shard')
            d['shards'][shard_id]=deepcopy(data)

    def put_quality_report(self, dataset_id, resource, job_id, owner, job_token, revision, token, report):
        with self.lock:
            self._owned(resource,job_id,owner,job_token,revision,token)
            self.c._charge(self.c._jobs[job_id],reads=1,writes=1)
            d=self.datasets[dataset_id]
            if (d['resource'],d['job_id'],d['owner'],d['token'],d['state']) != (resource,job_id,owner,token,'DRAFT'): raise Conflict('immutable or foreign report')
            if d['quality_report'] is not None and d['quality_report'] != report: raise Conflict('immutable report')
            d['quality_report']=deepcopy(report)

    def seal(self, dataset_id, resource, job_id, owner, job_token, revision, token, expected):
        with self.lock:
            self._owned(resource,job_id,owner,job_token,revision,token)
            self.c._charge(self.c._jobs[job_id],reads=1,writes=1)
            d=self.datasets[dataset_id]
            if (d['resource'],d['job_id'],d['owner'],d['token'],d['state']) != (resource,job_id,owner,token,'DRAFT'): raise Conflict('cannot seal')
            if not isinstance(expected,dict) or expected.get('manifest_version') != 'offline-manifest-v1': raise ValueError('strict manifest required')
            d['manifest']=deepcopy(expected); d['state']='VALIDATING'

    def verify(self, dataset_id, resource, job_id, owner, job_token, revision, token, *, quality_ok):
        with self.lock:
            self._owned(resource,job_id,owner,job_token,revision,token)
            self.c._charge(self.c._jobs[job_id],reads=2+len(self.datasets[dataset_id]["shards"]),writes=1)
            d=self.datasets[dataset_id]
            if (d['resource'],d['job_id'],d['owner'],d['token'],d['state']) != (resource,job_id,owner,token,'VALIDATING'): raise Conflict('wrong draft')
            actual={key:digest(value) for key,value in d['shards'].items()}
            if d['manifest'].get('manifest_version') == 'offline-manifest-v1':
                valid=validate_manifest(d['manifest'],dataset_id=dataset_id,job_id=job_id,resource_id=resource,
                                        metadata=d['metadata'],shards=d['shards'],hash_fn=digest)
                report=d.get('quality_report')
                valid=valid and isinstance(report,dict) and d['manifest'].get('quality_report') == report and d['manifest'].get('quality_report_digest') == digest(report)
                valid=valid and report.get('dataset_id') == dataset_id if isinstance(report,dict) else False
                valid=valid and report.get('source_id') == d['manifest'].get('source_id') and report.get('processing_version') == d['manifest'].get('processing_version') if isinstance(report,dict) else False
                snapshots=report.get('snapshots') if isinstance(report,dict) else None
                valid=valid and isinstance(snapshots,list) and bool(snapshots)
                if isinstance(snapshots,list):
                    valid=valid and any(isinstance(x,dict) and x.get('date') == d['manifest'].get('analysis_date') and x.get('source_url') == d['manifest'].get('provenance') for x in snapshots)
                for item in snapshots if isinstance(snapshots,list) else []:
                    if not isinstance(item,dict): valid=False; continue
                    counts=[item.get(k) for k in ('raw_count','accepted_count','rejected_count','duplicate_count')]
                    reasons=item.get('rejection_reasons'); flags=item.get('quality_flags')
                    valid=valid and bool(item.get('date')) and bool(item.get('status')) and bool(item.get('source_url')) and isinstance(reasons,dict) and isinstance(flags,list)
                    if any(x is None for x in counts): valid=valid and all(x is None for x in counts)
                    else:
                        valid=valid and all(type(x) is int and x >= 0 for x in counts) and counts[0] == sum(counts[1:])
                        valid=valid and isinstance(reasons,dict) and all(type(v) is int and v >= 0 for v in reasons.values()) and sum(reasons.values()) == counts[2]
                identity=self.c._jobs[job_id].identity
                scope=identity.get('scope', {})
                valid=valid and d['manifest'].get('occurrence_id') == identity.get('occurrence_id')
                valid=valid and d['manifest'].get('source_id') == identity.get('source')
                valid=valid and isinstance(scope,dict) and all(d['manifest'].get(k) == v for k,v in scope.items() if k in ('country','access','project'))
                try:
                    start=date.fromisoformat(d['manifest']['window_start']); end=date.fromisoformat(d['manifest']['window_end']); analysis=date.fromisoformat(d['manifest']['analysis_date'])
                    valid=valid and end == analysis and type(identity.get('window')) is int and (end-start).days == identity['window']
                except (ValueError, TypeError, KeyError): valid=False
                keys=('analysis_date','window_start','window_end','source_version','processing_version','cleaning_version','classification_version','algorithm_version','provenance')
                config=identity.get('dataset_config')
                valid=valid and isinstance(config,dict) and all(k in config and config[k] == d['manifest'].get(k) for k in keys)
                valid=valid and d['manifest'].get('config_version') == identity.get('config_version')
                valid=valid and all((k in d['manifest']) == (k in identity) and d['manifest'].get(k)==identity.get(k)
                                    and (k not in d['metadata'] or (k in identity and d['metadata'][k]==identity[k]))
                                    for k in ('schedule_id','schedule_version'))
                valid=valid and d['manifest'].get('source_id') == 'wikimedia_pageviews_top_per_country'
                if d['manifest'].get('source_id') == 'wikimedia_pageviews_top_per_country':
                    valid=valid and validate_source_evidence(d['manifest'],d['shards'],report)
                    entries=d['shards'].get('scores-v1',{}).get('items', [])
                    valid=valid and all(isinstance(row,dict) and row.get('source') == d['manifest']['source_id'] and row.get('country_scope') == d['manifest']['country'] and row.get('access') == d['manifest']['access'] and row.get('observation_date') == d['manifest']['analysis_date'] and row.get('algorithm_version') == d['manifest']['algorithm_version'] and not (row.get('status') == 'INSUFFICIENT_DATA' and row.get('score') is not None) for row in entries)
                if not valid:
                    d['state']='REJECTED'; return False
                d['verified_counts']={'shards':len(actual),'items':sum(len(v['items']) for v in d['shards'].values())}
                d['verified_identity']=digest(identity)
                d['verified']=digest({'manifest':d['manifest'],'metadata':d['metadata'],'counts':d['verified_counts'],'quality_report':d['quality_report']})
                d['verified_at']=self.c._now(); d['verification_version']='offline-verifier-v1'
                d['state']='READY'; return True
            d['state']='REJECTED'; return False

    def read_dataset(self, dataset_id):
        with self.lock:
            return deepcopy(self.datasets.get(dataset_id))

    def active(self,resource):
        with self.lock:
            r=self.resources.get(resource)
            return (r['active'],r['pointer_revision']) if r else (None,0)

    def replay_publication(self, resource, job_id, owner, dataset_id, fresh_on_retry, expected_active, expected_revision):
        """Read a committed receipt; never change the active pointer."""
        with self.lock:
            job=self.c._jobs[job_id]
            matches=[]
            for (stored_resource, stored_dataset), receipt in self.publications.items():
                published_job, published_owner, token, previous, previous_revision = receipt
                proposed=f'{dataset_id}-f{token}' if fresh_on_retry else dataset_id
                if (stored_resource,stored_dataset,published_job,published_owner,previous,previous_revision)==(resource,proposed,job_id,owner,expected_active,expected_revision):
                    matches.append((stored_dataset,token))
            if job.state != State.COMPLETED or len(matches) != 1: raise Conflict('publication receipt mismatch')
            self.c._charge(job,reads=1)
            stored_dataset,token=matches[0]
            return self.publish(stored_dataset,resource,job_id,owner,job.fencing,job.revision,token,expected_active,expected_revision)

    def publish(self, dataset_id, resource, job_id, owner, job_token, revision, token, expected_active, expected_revision):
        with self.lock:
            # A retry after a committed response lost can observe its own publication, never roll back a newer pointer.
            prior=self.publications.get((resource,dataset_id))
            if prior is not None:
                if prior != (job_id,owner,token,expected_active,expected_revision): raise Conflict('publication identity mismatch')
                self.c._charge(self.c._jobs[job_id],reads=1)
                return 'ALREADY_PUBLISHED'
            job, lease=self._owned(resource,job_id,owner,job_token,revision,token)
            d=self.datasets[dataset_id]
            # Publication rechecks every persisted shard, not just the pointer,
            # job and manifest. Account for those reads before any mutation.
            self.c._charge(job,reads=3+len(d['shards']),writes=3)
            if job.state != State.VALIDATING or d['state'] != 'READY' or (d['resource'],d['job_id'],d['owner'],d['token']) != (resource,job_id,owner,token): raise Conflict('not ready')
            if d.get('verified_identity') != digest(job.identity): raise Conflict('job identity changed')
            if d['verified'] != digest({'manifest':d['manifest'],'metadata':d['metadata'],'counts':d['verified_counts'],'quality_report':d['quality_report']}): raise Conflict('manifest changed')
            if not isinstance(d.get('manifest'),dict) or d['manifest'].get('manifest_version') != 'offline-manifest-v1': raise Conflict('strict manifest required')
            if not validate_manifest(d['manifest'],dataset_id=dataset_id,job_id=job_id,resource_id=resource,metadata=d['metadata'],shards=d['shards'],hash_fn=digest): raise Conflict('persisted content changed')
            if (lease['active'],lease['pointer_revision']) != (expected_active,expected_revision): raise Conflict('pointer CAS')
            # Prepare all fallible work before mutating shared publication state.
            # The common lock is the fake atomic boundary; this is not disk durability.
            updated_job=deepcopy(job)
            updated_job.state=State.COMPLETED; updated_job.reason_code=None
            self.c._record(updated_job,self.c._now(),'PUBLISHED')
            if expected_active is not None: self.datasets[expected_active]['state']='SUPERSEDED'
            lease['active']=dataset_id; lease['pointer_revision']+=1
            d['state']='ACTIVE'; self.c._jobs[job_id]=updated_job
            self.publications[resource,dataset_id]=(job_id,owner,token,expected_active,expected_revision)
            return 'PUBLISHED'
