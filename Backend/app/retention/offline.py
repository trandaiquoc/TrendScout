"""Manifest-driven cleanup prototype over the Step 14 in-process fake only.

There is no Firestore adapter, runtime flag, network access, or public route.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import re

from app.datasets.fake import FakeDatasetStore, digest
from app.datasets.manifest import validate_manifest


@dataclass(frozen=True)
class Policy:
    policy_id: str
    version: str
    retention_days: int
    grace_days: int


# Simulation assumptions only; neither is an approved production policy.
STRICT_RETENTION = Policy('STRICT_RETENTION', 'simulation-v1', 7, 0)
GRACE_FALLBACK = Policy('GRACE_FALLBACK', 'simulation-v1', 7, 7)
DRY_RUN = True
LIVE_DELETE_ENABLED = False
# Non-dataset classes have no simulated automatic deletion deadline. A baseline
# embedded in results remains protected while its explicit baseline reference exists.
DATA_CLASS_POLICY = {
    'dataset_results':'policy-retention', 'raw_observations':'policy-or-license',
    'source_snapshots':'policy-or-license', 'algorithm_baselines':'reference-protected',
    'quality_reports':'policy-retention', 'job_metadata':None, 'audit_records':None,
    'tenant_specific':None, 'saved_user_research':None,
}
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_FIELDS = frozenset({'resource','job_id','owner','token','metadata','state','shards',
    'quality_report','manifest','verified','verified_counts','verified_identity',
    'verified_at','verification_version'})
_DERIVED_FIELDS = frozenset({'keyword_id','score','status','algorithm_version'})
_SAFE_REPORT_FIELDS = frozenset({'policy_version','status','observed_count','keyword_count','source_id','processing_version','snapshots','dataset_id'})
_SAFE_DAY_FIELDS = frozenset({'date','status','raw_count','accepted_count','rejected_count','duplicate_count','rejection_reasons','quality_flags','source_url'})
_SAFE_METADATA_FIELDS = frozenset({'access','algorithm_version','analysis_date','classification_version','cleaning_version','config_version','country','occurrence_id','processing_version','project','provenance','quality_report_ref','source_id','source_version','window_end','window_start'})
_SAFE_MANIFEST_FIELDS = _SAFE_METADATA_FIELDS | frozenset({'dataset_id','expected_count','job_id','manifest_version','quality_report','quality_report_digest','required_partitions','resource_id','shards'})
_SCHEDULE_FIELDS = frozenset({'schedule_id','schedule_version'})
_SAFE_IDENTITY_FIELDS = frozenset({'job_type','source','scope','window','config_version','occurrence_id','tenant_scope','resource_id','dataset_config'})
_SAFE_CONFIG_FIELDS = frozenset({'analysis_date','window_start','window_end','source_version','processing_version','cleaning_version','classification_version','algorithm_version','provenance'})
_KNOWN_SHARDS = frozenset({'raw-observations-v1','snapshots-v1','scores-v1'})


@dataclass(frozen=True)
class CleanupManifest:
    version: str
    dataset_id: str
    resource_id: str
    source_manifest_fingerprint: str
    document_paths: tuple[str, ...]


@dataclass(frozen=True)
class CleanupPlan:
    plan_id: str
    dataset_id: str
    resource_id: str
    policy_id: str
    policy_version: str
    target: str
    eligible_at: str
    published_at: str | None
    manifest_fingerprint: str
    metadata_fingerprint: str
    identity_fingerprint: str
    pointer_revision: int
    resource_token: int
    resource_owner: str | None
    protected_version: int
    prior_deleted_shards: tuple[str, ...]
    prior_redactions: tuple[tuple[str,str], ...]
    cleanup_manifest: CleanupManifest
    inventory_paths: tuple[str, ...]
    paths: tuple[str, ...]
    actions: tuple[str, ...]
    estimated_reads: int
    estimated_writes: int
    estimated_deletes: int
    max_batch_size: int
    max_reads: int
    max_writes: int
    max_deletes: int
    max_retries: int
    deadline: str
    audit_reference: str
    eligible: bool
    reason: str

    @property
    def freshness(self):
        return 'STALE' if self.reason=='ACTIVE_GRACE' else 'PENDING_CLEANUP' if self.eligible else 'UNCHANGED'

    @property
    def operator_approval_required(self):
        return True


@dataclass
class Checkpoint:
    plan_id: str
    dataset_id: str
    state: str = 'PLANNED'
    cursor: int = 0
    completed_paths: list[str] = field(default_factory=list)
    redacted_digests: dict[str,str] = field(default_factory=dict)
    retries: int = 0
    last_error: str | None = None
    reads: int = 0
    writes: int = 0
    deletes: int = 0
    pointer_revision: int = 0
    last_successful_path: str | None = None
    retry_after: str | None = None
    inflight: bool = False
    history: list[str] = field(default_factory=lambda:['PLANNED'])


@dataclass(frozen=True)
class Limits:
    reads: int = 500
    writes: int = 200
    deletes: int = 100
    batch_size: int = 25
    retries: int = 3

    def __post_init__(self):
        if any(type(x) is not int or not 1 <= x <= bound for x,bound in
               ((self.reads,1000),(self.writes,1000),(self.deletes,1000),(self.batch_size,25),(self.retries,5))):
            raise ValueError('bounded integer cleanup limits required')


class FakeCleanup:
    """Explicit fake execution only; lock is shared with the fake publisher."""
    def __init__(self, repository, clock, *, fault_hook=None):
        if type(repository) is not FakeDatasetStore: raise ValueError('fake dataset repository required')
        self.repo=repository; self.clock=clock
        self.fault_hook=fault_hook or (lambda _point:None)
        with repository.lock:
            store=repository.c._store
            self.storage=store.retention_state

    def set_protection(self, resource, dataset, **changes):
        """Model only fake reference/hold changes under the publisher lock."""
        allowed={'hold','shared','baseline','license_clear','license_hold','legal_delete','license_raw_due','refresh_failed'}
        if not changes or set(changes)-allowed or any(type(v) is not bool for v in changes.values()):
            raise ValueError('invalid protection')
        with self.repo.lock:
            if not isinstance(resource,str) or not _ID.fullmatch(resource) or not isinstance(dataset,str) or not _ID.fullmatch(dataset):
                raise ValueError('invalid canonical identity')
            d=self.repo.datasets.get(dataset)
            if not d or d['resource']!=resource: raise ValueError('dataset/resource mismatch')
            if d['state'] in ('CLEANING','PARTIALLY_DELETED') and any(changes.get(k) for k in ('shared','baseline')):
                raise ValueError('cannot reference a partially deleted dataset')
            refs=self.storage['references'].setdefault((resource,dataset),{})
            refs.update(changes); self.storage['reference_version']+=1

    def _change_reference(self, resource, dataset, kind, reference, *, add):
        if kind not in ('shared','baseline') or not isinstance(reference,str) or not _ID.fullmatch(reference):
            raise ValueError('invalid reference identity')
        with self.repo.lock:
            if not isinstance(resource,str) or not _ID.fullmatch(resource) or not isinstance(dataset,str) or not _ID.fullmatch(dataset):
                raise ValueError('invalid canonical identity')
            d=self.repo.datasets.get(dataset)
            if not d or d['resource']!=resource: raise ValueError('dataset/resource mismatch')
            if add and d['state'] in ('CLEANING','PARTIALLY_DELETED'): raise ValueError('dataset unavailable')
            refs=self.storage['references'].setdefault((resource,dataset),{})
            members=refs.setdefault(kind+'_refs',set())
            if add:
                if reference not in members: members.add(reference); self.storage['reference_version']+=1
            else:
                if reference not in members: raise ValueError('unknown reference')
                members.remove(reference); self.storage['reference_version']+=1

    def add_reference(self, resource, dataset, kind, reference):
        self._change_reference(resource,dataset,kind,reference,add=True)

    def remove_reference(self, resource, dataset, kind, reference):
        self._change_reference(resource,dataset,kind,reference,add=False)

    def _published_at(self, job):
        entries=[item for item in job.audit if item.get('action')=='PUBLISHED' and item.get('state')=='COMPLETED']
        if len(entries)!=1 or not isinstance(entries[0].get('at'),datetime): raise ValueError('publication time unavailable')
        published=entries[0]['at']
        if published.tzinfo is None or published.utcoffset()!=timedelta(0) or published>self.clock():
            raise ValueError('invalid publication time')
        return published

    def _inspect(self, resource, dataset, policy):
        if not isinstance(resource,str) or not _ID.fullmatch(resource) or not isinstance(dataset,str) or not _ID.fullmatch(dataset):
            raise ValueError('invalid canonical identity')
        if policy not in (STRICT_RETENTION,GRACE_FALLBACK): raise ValueError('unapproved simulation policy')
        d=self.repo.datasets.get(dataset); lease=self.repo.resources.get(resource)
        if not d or d.get('resource') != resource or not lease: raise ValueError('dataset/resource mismatch')
        if set(d)-_FIELDS: raise ValueError('unknown dataset field or subcollection')
        manifest=d.get('manifest'); shards=d.get('shards'); metadata=d.get('metadata')
        if not isinstance(manifest,dict) or not isinstance(shards,dict) or not isinstance(metadata,dict): raise ValueError('missing manifest')
        if not isinstance(manifest.get('shards'),dict) or set(manifest['shards'])!=_KNOWN_SHARDS:
            raise ValueError('unknown or missing shard contract')
        if any(not isinstance(k,str) or not _ID.fullmatch(k) for k in shards): raise ValueError('invalid shard id')
        prior_removed=(); prior_redactions=()
        if d['state']=='PARTIALLY_DELETED':
            proofs=[(self.storage['plans'][key],run) for key,run in self.storage['runs'].items()
                    if run.dataset_id==dataset and run.state=='PARTIALLY_DELETED' and self.storage['plans'][key].target=='RAW_ONLY']
            if len(proofs)!=1: raise ValueError('partial dataset lacks receipt')
            prior,receipt=proofs[0]
            if prior.resource_id!=resource or prior.manifest_fingerprint!=digest(manifest) or receipt.completed_paths!=list(prior.paths):
                raise ValueError('partial receipt mismatch')
            removed={path.split('/')[-1] for path,action in zip(prior.paths,prior.actions) if action=='DELETE'}
            prior_removed=tuple(sorted(removed))
            prior_redactions=tuple(sorted(receipt.redacted_digests.items()))
            expected_redactions={path.split('/')[-1] for path,action in zip(prior.paths,prior.actions) if action=='REDACT'}
            if set(receipt.redacted_digests)!=expected_redactions: raise ValueError('partial redaction receipt mismatch')
            if set(shards)!=set(manifest.get('shards',{}))-removed: raise ValueError('partial shard mismatch')
            if any(digest(value)!=receipt.redacted_digests.get(key,manifest['shards'][key]['sha256']) for key,value in shards.items()): raise ValueError('partial shard changed')
            for key in expected_redactions:
                if any(not isinstance(row,dict) or set(row)!=_DERIVED_FIELDS for row in shards[key].get('items',())):
                    raise ValueError('raw content remains in derived shard')
        elif not validate_manifest(manifest,dataset_id=dataset,job_id=d['job_id'],resource_id=resource,metadata=metadata,shards=shards,hash_fn=digest):
            raise ValueError('invalid manifest')
        job=self.repo.c._jobs.get(d['job_id'])
        if job is None or job.identity.get('resource_id') != resource: raise ValueError('invalid job identity')
        identity=job.identity; config=identity.get('dataset_config',{}); scope=identity.get('scope',{})
        if (identity.get('job_type')!='analysis' or identity.get('tenant_scope')!='public' or
            identity.get('source')!=manifest.get('source_id') or identity.get('occurrence_id')!=manifest.get('occurrence_id') or
            identity.get('config_version')!=manifest.get('config_version') or not isinstance(scope,dict) or
            any(scope.get(k)!=manifest.get(k) for k in ('country','access','project')) or
            not isinstance(config,dict) or any(config.get(k)!=manifest.get(k) for k in
                ('analysis_date','window_start','window_end','source_version','processing_version','cleaning_version','classification_version','algorithm_version','provenance'))):
            raise ValueError('identity scope or version mismatch')
        if d.get('quality_report') != manifest.get('quality_report') or digest(d['quality_report']) != manifest.get('quality_report_digest'):
            raise ValueError('invalid quality report')
        report=d['quality_report']
        safe_report=(isinstance(report,dict) and set(report)==_SAFE_REPORT_FIELDS and isinstance(report.get('snapshots'),list)
                     and all(isinstance(x,dict) and set(x)==_SAFE_DAY_FIELDS for x in report['snapshots']))
        safe_metadata=(_SAFE_METADATA_FIELDS<=set(metadata)<=_SAFE_METADATA_FIELDS|_SCHEDULE_FIELDS and
                       _SAFE_MANIFEST_FIELDS<=set(manifest)<=_SAFE_MANIFEST_FIELDS|_SCHEDULE_FIELDS and
                       _SAFE_IDENTITY_FIELDS<=set(identity)<=_SAFE_IDENTITY_FIELDS|_SCHEDULE_FIELDS and
                       set(config)==_SAFE_CONFIG_FIELDS and set(scope)=={'country','access','project'} and
                       all(isinstance(spec,dict) and set(spec)=={'count','sha256'} for spec in manifest['shards'].values()))
        if not safe_report or not safe_metadata: raise ValueError('unknown metadata or quality contract')
        if d.get('verified_identity')!=digest(job.identity) or d.get('verified')!=digest({
            'manifest':manifest,'metadata':metadata,'counts':d.get('verified_counts'),'quality_report':d['quality_report']}):
            raise ValueError('unverified or changed identity')
        if d['state'] in ('ACTIVE','SUPERSEDED','PARTIALLY_DELETED'):
            receipt=self.repo.publications.get((resource,dataset))
            if receipt is None or receipt[:3]!=(d['job_id'],d['owner'],d['token']): raise ValueError('missing publication receipt')
        published_at=self._published_at(job) if d['state'] in ('ACTIVE','SUPERSEDED','PARTIALLY_DELETED') else None
        age=self.clock()-published_at if published_at is not None else timedelta(0)
        active=lease['active']==dataset
        if ((d['state']=='ACTIVE') != active and d['state']!='READY') or (d['state']=='PARTIALLY_DELETED' and active):
            raise ValueError('active pointer and dataset state disagree')
        refs=self.storage['references'].get((resource,dataset),{})
        protected=any(refs.get(k) for k in ('hold','license_hold','shared','baseline','shared_refs','baseline_refs'))
        if (refs.get('legal_delete') or refs.get('license_raw_due')) and protected: reason='POLICY_CONFLICT'
        elif refs.get('hold') or refs.get('license_hold'): reason='ON_HOLD'
        elif any(refs.get(k) for k in ('shared','baseline','shared_refs','baseline_refs')): reason='PROTECTED_REFERENCE'
        elif not refs.get('license_clear'): reason='LICENSE_UNKNOWN'
        elif d['state'] not in ('ACTIVE','SUPERSEDED','READY','PARTIALLY_DELETED'): reason='INELIGIBLE_STATE'
        elif d['state']=='READY': reason='PUBLISH_PENDING'
        elif refs.get('legal_delete'): reason='ELIGIBLE_LEGAL'
        elif refs.get('license_raw_due') and d['state']!='PARTIALLY_DELETED': reason='ELIGIBLE_LICENSE_RAW'
        elif refs.get('license_raw_due'): reason='POLICY_CONFLICT'
        elif age < timedelta(days=policy.retention_days): reason='NOT_EXPIRED'
        elif active and policy.grace_days and age < timedelta(days=policy.retention_days+policy.grace_days):
            reason='ACTIVE_GRACE' if refs.get('refresh_failed') else 'ACTIVE_GRACE_UNVERIFIED'
        elif isinstance(shards.get('scores-v1'),dict) and shards['scores-v1'].get('items')==[]: reason='ELIGIBLE_EMPTY'
        else: reason='ELIGIBLE'
        root=f'analysis_datasets/{dataset}'
        target='RAW_ONLY' if reason=='ELIGIBLE_LICENSE_RAW' else 'DATASET'
        if target=='RAW_ONLY':
            required=_KNOWN_SHARDS
            safe_scores=(isinstance(shards.get('scores-v1'),dict) and set(shards['scores-v1'])=={'items'} and
                         isinstance(shards['scores-v1']['items'],list) and all(isinstance(row,dict) and _DERIVED_FIELDS<=row.keys() for row in shards['scores-v1']['items']))
            if set(shards)!=required or not safe_report or not safe_scores or not safe_metadata: reason='POLICY_CONFLICT'; paths=(); actions=()
            else:
                paths=tuple(f'{root}/shards/{k}' for k in ('scores-v1','raw-observations-v1','snapshots-v1'))
                actions=('REDACT','DELETE','DELETE')
        else:
            paths=tuple([f'{root}/shards/{k}' for k in sorted(shards)]+[f'{root}/quality/report',root])
            actions=('DELETE',)*len(paths)
        return d,lease,job,reason,paths,actions,target,prior_removed,prior_redactions,published_at.isoformat() if published_at else None

    def plan(self, resource, dataset, policy, *, limits=Limits()):
        if not isinstance(limits,Limits): raise ValueError('cleanup limits required')
        self.fault_hook('before_plan')
        with self.repo.lock:
            d,lease,job,reason,paths,actions,target,prior_removed,prior_redactions,published_at=self._inspect(resource,dataset,policy)
            stamp=self.clock(); fingerprint=digest(d['manifest']); version=self.storage['reference_version']
            root=f'analysis_datasets/{dataset}'
            inventory=tuple([f'{root}/shards/{k}' for k in sorted(d['manifest']['shards'])]+[root+'/quality/report',root])
            cleanup_manifest=CleanupManifest('cleanup-inventory-v1',dataset,resource,fingerprint,inventory)
            value={'dataset':dataset,'resource':resource,'policy':policy.policy_id,'version':policy.version,'target':target,
                   'at':stamp.isoformat(),'published_at':published_at,'manifest':fingerprint,'metadata':digest(d['metadata']),
                   'identity':digest(job.identity),'revision':lease['pointer_revision'],
                   'resource_token':lease['token'],'resource_owner':lease['owner'],
                   'references':version,'prior_deleted':prior_removed,'prior_redactions':prior_redactions,'cleanup_manifest':asdict(cleanup_manifest),'inventory':inventory,'paths':paths,'actions':actions,
                   'limits':(limits.reads,limits.writes,limits.deletes,limits.batch_size,limits.retries)}
            plan_id=digest(value)
            plan=CleanupPlan(plan_id,dataset,resource,policy.policy_id,policy.version,target,stamp.isoformat(),published_at,fingerprint,
                digest(d['metadata']),digest(job.identity),lease['pointer_revision'],lease['token'],lease['owner'],
                version,prior_removed,prior_redactions,cleanup_manifest,inventory,paths,actions,3*(3+len(inventory))+len(paths)*(4*(3+len(inventory)))+limits.retries+2,
                10+9*len(paths)+2*limits.retries,actions.count('DELETE'),limits.batch_size,
                limits.reads,limits.writes,limits.deletes,limits.retries,
                (stamp+timedelta(hours=1)).isoformat(),'cleanup:'+plan_id,reason in ('ELIGIBLE','ELIGIBLE_EMPTY','ELIGIBLE_LEGAL','ELIGIBLE_LICENSE_RAW'),reason)
        self.fault_hook('after_plan')
        return plan

    def list_candidates(self, policy, *, limits=Limits()):
        """Dry-run enumeration, including datasets rejected during validation."""
        with self.repo.lock:
            candidates=[(key,value.get('resource')) for key,value in self.repo.datasets.items() if isinstance(value,dict)]
            result=[]
            for dataset,resource in sorted(candidates):
                try:
                    item=self.plan(resource,dataset,policy,limits=limits)
                    result.append({'dataset_id':dataset,'eligible':item.eligible,'reason':item.reason,'plan':item})
                except (ValueError,KeyError,TypeError):
                    result.append({'dataset_id':dataset,'eligible':False,'reason':'INVALID_DATASET','plan':None})
            return result

    def start(self, plan, *, execute_fake=False):
        if type(execute_fake) is not bool: raise ValueError('explicit boolean fake execution required')
        if not execute_fake: return Checkpoint(plan.plan_id,plan.dataset_id)
        policies={p.policy_id:p for p in (STRICT_RETENTION,GRACE_FALLBACK)}
        with self.repo.lock:
            if plan.plan_id in self.storage['runs']:
                if self.storage['plans'][plan.plan_id] != plan: raise ValueError('plan identity conflict')
                return deepcopy(self.storage['runs'][plan.plan_id])
            policy=policies.get(plan.policy_id)
            if policy is None or plan.policy_version != policy.version or not plan.eligible: raise ValueError('ineligible plan')
            limits=Limits(plan.max_reads,plan.max_writes,plan.max_deletes,plan.max_batch_size,plan.max_retries)
            stamp=datetime.fromisoformat(plan.eligible_at)
            value={'dataset':plan.dataset_id,'resource':plan.resource_id,'policy':plan.policy_id,'version':plan.policy_version,'target':plan.target,
                   'at':plan.eligible_at,'published_at':plan.published_at,'manifest':plan.manifest_fingerprint,'metadata':plan.metadata_fingerprint,
                   'identity':plan.identity_fingerprint,'revision':plan.pointer_revision,
                   'resource_token':plan.resource_token,'resource_owner':plan.resource_owner,
                   'references':plan.protected_version,'prior_deleted':plan.prior_deleted_shards,'prior_redactions':plan.prior_redactions,
                   'cleanup_manifest':asdict(plan.cleanup_manifest),'inventory':plan.inventory_paths,
                   'paths':plan.paths,'actions':plan.actions,
                   'limits':(limits.reads,limits.writes,limits.deletes,limits.batch_size,limits.retries)}
            if (digest(value)!=plan.plan_id or plan.audit_reference!='cleanup:'+plan.plan_id or
                plan.deadline!=(stamp+timedelta(hours=1)).isoformat() or self.clock()>=datetime.fromisoformat(plan.deadline) or
                stamp>self.clock() or plan.reason not in ('ELIGIBLE','ELIGIBLE_EMPTY','ELIGIBLE_LEGAL','ELIGIBLE_LICENSE_RAW') or
                plan.cleanup_manifest!=CleanupManifest('cleanup-inventory-v1',plan.dataset_id,plan.resource_id,plan.manifest_fingerprint,plan.inventory_paths) or
                (plan.estimated_reads,plan.estimated_writes,plan.estimated_deletes)!=(3*(3+len(plan.inventory_paths))+len(plan.paths)*(4*(3+len(plan.inventory_paths)))+limits.retries+2,10+9*len(plan.paths)+2*limits.retries,plan.actions.count('DELETE'))):
                raise ValueError('invalid plan contract')
            # Reserve the planning read estimate and both eligibility checks;
            # never begin fake execution when even its guard budget is absent.
            start_reads=3*(3+len(plan.inventory_paths)); start_writes=4
            if start_reads+1>plan.max_reads or start_writes+2>plan.max_writes:
                return Checkpoint(plan.plan_id,plan.dataset_id,state='PAUSED',last_error='QUOTA_EXCEEDED')
            d,lease,job,reason,paths,actions,target,prior_removed,prior_redactions,published_at=self._inspect(plan.resource_id,plan.dataset_id,policy)
            root=f'analysis_datasets/{plan.dataset_id}'
            inventory=tuple([f'{root}/shards/{k}' for k in sorted(d['manifest']['shards'])]+[root+'/quality/report',root])
            if (reason!=plan.reason or digest(d['manifest']) != plan.manifest_fingerprint or
                digest(d['metadata'])!=plan.metadata_fingerprint or digest(job.identity)!=plan.identity_fingerprint or
                lease['pointer_revision'] != plan.pointer_revision or lease['token']!=plan.resource_token or lease['owner']!=plan.resource_owner or
                self.storage['reference_version'] != plan.protected_version or inventory!=plan.inventory_paths or paths!=plan.paths or actions!=plan.actions or target!=plan.target or prior_removed!=plan.prior_deleted_shards or prior_redactions!=plan.prior_redactions or published_at!=plan.published_at):
                raise ValueError('stale plan')
            self.fault_hook('after_validation')
            d,lease,job,reason,paths,actions,target,prior_removed,prior_redactions,published_at=self._inspect(plan.resource_id,plan.dataset_id,policy)
            inventory=tuple([f'{root}/shards/{k}' for k in sorted(d['manifest']['shards'])]+[root+'/quality/report',root])
            if (reason!=plan.reason or digest(d['manifest'])!=plan.manifest_fingerprint or digest(d['metadata'])!=plan.metadata_fingerprint or
                digest(job.identity)!=plan.identity_fingerprint or lease['pointer_revision']!=plan.pointer_revision or
                lease['token']!=plan.resource_token or lease['owner']!=plan.resource_owner or
                self.storage['reference_version']!=plan.protected_version or inventory!=plan.inventory_paths or paths!=plan.paths or actions!=plan.actions or target!=plan.target or
                prior_removed!=plan.prior_deleted_shards or prior_redactions!=plan.prior_redactions or published_at!=plan.published_at or self.clock()>=datetime.fromisoformat(plan.deadline)):
                raise ValueError('stale plan after validation')
            if lease['active']==plan.dataset_id:
                lease['active']=None; lease['pointer_revision']+=1
            d['state']='CLEANING'
            checkpoint=Checkpoint(plan.plan_id,plan.dataset_id,state='READY',pointer_revision=lease['pointer_revision'],reads=start_reads,writes=start_writes,history=['PLANNED','VALIDATING','READY'])
            self.storage['plans'][plan.plan_id]=plan
            self.storage['runs'][plan.plan_id]=checkpoint
            self.storage['audit'].append((plan.plan_id,'RESERVED',None))
            return deepcopy(checkpoint)

    def step(self, plan, checkpoint, *, max_deletes=25):
        if type(max_deletes) is not int or not 1 <= max_deletes <= plan.max_batch_size: raise ValueError('invalid batch')
        with self.repo.lock:
            state=self.storage['runs'].get(plan.plan_id)
            if (state is None or state.dataset_id!=plan.dataset_id or checkpoint.plan_id!=plan.plan_id
                or self.storage['plans'].get(plan.plan_id)!=plan): raise ValueError('unknown execution or altered plan')
            if checkpoint.cursor>state.cursor or checkpoint.completed_paths!=list(plan.paths[:checkpoint.cursor]):
                raise ValueError('invalid checkpoint replay')
            if checkpoint.cursor<state.cursor: return deepcopy(state)
            if state.state in ('COMPLETED','PARTIALLY_DELETED'): return deepcopy(state)
            if state.state in ('PAUSED','FAILED','BLOCKED'): return deepcopy(state)
            if state.inflight: return self.schedule_retry(plan,error_code='TRANSIENT_FAKE_ERROR')
            if state.state=='RETRY_WAIT' and self.clock()<datetime.fromisoformat(state.retry_after): return deepcopy(state)
            def expired():
                if self.clock()<datetime.fromisoformat(plan.deadline): return None
                if state.writes+2<=plan.max_writes:
                    state.state='FAILED'; state.last_error='DEADLINE_EXPIRED'; state.inflight=False; state.writes+=2; state.history.append('FAILED')
                    self.storage['audit'].append((plan.plan_id,'DEADLINE_EXPIRED',None))
                    return deepcopy(state)
                result=deepcopy(state); result.state='FAILED'; result.last_error='DEADLINE_EXPIRED'; return result
            failure=expired()
            if failure is not None: return failure
            validation_reads=3+len(plan.inventory_paths)
            if state.reads+validation_reads+1>plan.max_reads or state.writes+4>plan.max_writes:
                if state.writes+2<=plan.max_writes:
                    state.state='PAUSED'; state.last_error='QUOTA_EXCEEDED'; state.writes+=2; state.history.append('PAUSED')
                    self.storage['audit'].append((plan.plan_id,'QUOTA_EXCEEDED',None))
                paused=deepcopy(state); paused.state='PAUSED'; paused.last_error='QUOTA_EXCEEDED'; return paused
            state.reads+=validation_reads; state.writes+=2
            self.storage['audit'].append((plan.plan_id,'VALIDATION_STARTED',None))
            def blocked(code):
                if state.writes+2<=plan.max_writes:
                    state.state='BLOCKED'; state.last_error=code; state.inflight=False; state.writes+=2; state.history.append('BLOCKED')
                    self.storage['audit'].append((plan.plan_id,code,None))
                raise ValueError(code)
            d=self.repo.datasets.get(plan.dataset_id); lease=self.repo.resources[plan.resource_id]
            job=self.repo.c._jobs.get(d['job_id']) if d else None
            if (not d or set(d)-_FIELDS or d['state']!='CLEANING' or lease['active']==plan.dataset_id
                or lease['pointer_revision']!=state.pointer_revision
                or lease['token']!=plan.resource_token or lease['owner']!=plan.resource_owner
                or self.storage['reference_version']!=plan.protected_version
                or digest(d['manifest'])!=plan.manifest_fingerprint
                or digest(d['metadata'])!=plan.metadata_fingerprint or not job or digest(job.identity)!=plan.identity_fingerprint
                or self._published_at(job).isoformat()!=plan.published_at):
                blocked('SAFETY_REVALIDATION_FAILED')
            root=f'analysis_datasets/{plan.dataset_id}'
            if state.completed_paths != list(plan.paths[:state.cursor]): blocked('INVALID_CHECKPOINT')
            remaining=set(plan.paths[state.cursor:])
            processed=list(zip(plan.paths[:state.cursor],plan.actions[:state.cursor]))
            removed_shards={parts[3] for p,action in processed if action=='DELETE' and len(parts:=p.split('/'))==4
                            and parts[:3]==['analysis_datasets',plan.dataset_id,'shards']}
            redacted_shards={p.split('/')[-1] for p,action in processed if action=='REDACT'}
            if set(state.redacted_digests)!=redacted_shards: blocked('INVALID_REDACTION_RECEIPT')
            expected_digests=dict(plan.prior_redactions); expected_digests.update(state.redacted_digests)
            expected_shards=set(d['manifest']['shards'])-removed_shards-set(plan.prior_deleted_shards)
            if set(d['shards'])!=expected_shards or any(digest(value)!=expected_digests.get(key,d['manifest']['shards'][key]['sha256']) for key,value in d['shards'].items()):
                blocked('PERSISTED_SHARDS_CHANGED')
            if root+'/quality/report' not in state.completed_paths:
                if digest(d['quality_report'])!=d['manifest']['quality_report_digest']: blocked('QUALITY_REPORT_CHANGED')
            elif d['quality_report'] is not None: blocked('QUALITY_REPORT_UNEXPECTED')
            remaining_deletes=plan.max_deletes-state.deletes
            remaining_writes=plan.max_writes-state.writes
            remaining_reads=plan.max_reads-state.reads
            batch_reads=3+len(expected_shards)+(root+'/quality/report' in remaining)
            count=min(max_deletes,remaining_deletes+1,max(0,(remaining_writes-6)//3),max(0,(remaining_reads-batch_reads-1)//(2*batch_reads)))
            if count <= 0:
                if remaining_writes>=2:
                    state.state='PAUSED'; state.last_error='QUOTA_EXCEEDED'; state.writes+=2; state.history.append('PAUSED')
                    self.storage['audit'].append((plan.plan_id,'QUOTA_EXCEEDED',None))
                paused=deepcopy(state); paused.state='PAUSED'; paused.last_error='QUOTA_EXCEEDED'; return paused
            state.reads+=batch_reads
            state.writes+=2; state.inflight=True; state.state='DELETING'; state.retry_after=None
            state.history.append('DELETING')
            self.storage['audit'].append((plan.plan_id,'BATCH_STARTED',None))
            self.fault_hook('before_batch')
            for path in plan.paths[state.cursor:state.cursor+count]:
                action=plan.actions[state.cursor]
                if action=='DELETE' and state.deletes>=plan.max_deletes:
                    state.state='PAUSED'; state.last_error='QUOTA_EXCEEDED'; state.inflight=False; state.writes+=2; state.history.append('PAUSED')
                    self.storage['audit'].append((plan.plan_id,'QUOTA_EXCEEDED',None)); return deepcopy(state)
                failure=expired()
                if failure is not None: return failure
                state.reads+=2*batch_reads
                if (lease['pointer_revision']!=state.pointer_revision or lease['active']==plan.dataset_id or
                    lease['token']!=plan.resource_token or lease['owner']!=plan.resource_owner or
                    self.storage['reference_version']!=plan.protected_version or set(d)-_FIELDS or
                    digest(d['manifest'])!=plan.manifest_fingerprint or digest(d['metadata'])!=plan.metadata_fingerprint or
                    digest(job.identity)!=plan.identity_fingerprint or self._published_at(job).isoformat()!=plan.published_at): blocked('CLEANUP_FENCING_CHANGED')
                processed=list(zip(plan.paths[:state.cursor],plan.actions[:state.cursor]))
                removed_shards={parts[3] for p,kind in processed if kind=='DELETE' and len(parts:=p.split('/'))==4
                                and parts[:3]==['analysis_datasets',plan.dataset_id,'shards']}
                expected_digests=dict(plan.prior_redactions); expected_digests.update(state.redacted_digests)
                expected_shards=set(d['manifest']['shards'])-removed_shards-set(plan.prior_deleted_shards)
                if set(d['shards'])!=expected_shards or any(digest(value)!=expected_digests.get(key,d['manifest']['shards'][key]['sha256']) for key,value in d['shards'].items()):
                    blocked('PERSISTED_SHARDS_CHANGED')
                if root+'/quality/report' not in state.completed_paths:
                    if digest(d['quality_report'])!=d['manifest']['quality_report_digest']: blocked('QUALITY_REPORT_CHANGED')
                elif d['quality_report'] is not None: blocked('QUALITY_REPORT_UNEXPECTED')
                parts=path.split('/')
                if action=='REDACT' and plan.target=='RAW_ONLY' and path==root+'/shards/scores-v1':
                    values=d['shards']['scores-v1']['items']
                    if any(not isinstance(row,dict) or not _DERIVED_FIELDS<=row.keys() for row in values): raise ValueError('invalid derived projection')
                    d['shards']['scores-v1']={'items':[{key:row[key] for key in sorted(_DERIVED_FIELDS)} for row in values]}
                    state.redacted_digests['scores-v1']=digest(d['shards']['scores-v1'])
                elif action!='DELETE': raise ValueError('invalid cleanup action')
                elif path==root:
                    if d['shards'] or d['quality_report'] is not None: raise ValueError('children remain')
                    self.fault_hook('before_parent')
                    failure=expired()
                    if failure is not None: return failure
                    if (set(d)-_FIELDS or d['state']!='CLEANING' or d['shards'] or d['quality_report'] is not None or
                        lease['pointer_revision']!=state.pointer_revision or lease['active']==plan.dataset_id or
                        lease['token']!=plan.resource_token or lease['owner']!=plan.resource_owner or
                        self.storage['reference_version']!=plan.protected_version or
                        digest(d['manifest'])!=plan.manifest_fingerprint or digest(d['metadata'])!=plan.metadata_fingerprint or
                        digest(job.identity)!=plan.identity_fingerprint or self._published_at(job).isoformat()!=plan.published_at): blocked('PARENT_REVALIDATION_FAILED')
                    del self.repo.datasets[plan.dataset_id]
                elif len(parts)==4 and parts[:3]==['analysis_datasets',plan.dataset_id,'shards'] and _ID.fullmatch(parts[3]):
                    del d['shards'][parts[3]]
                elif path==root+'/quality/report': d['quality_report']=None
                else: raise ValueError('path outside allowlist')
                state.completed_paths.append(path); state.cursor+=1; state.deletes+=action=='DELETE'; state.writes+=3 if action=='REDACT' else 2
                state.last_successful_path=path
                self.storage['audit'].append((plan.plan_id,'REDACTED' if action=='REDACT' else 'DELETED',path))
                if state.cursor==len(plan.paths):
                    state.state='PARTIALLY_DELETED' if plan.target=='RAW_ONLY' else 'COMPLETED'
                    if plan.target=='RAW_ONLY': d['state']='PARTIALLY_DELETED'
                    state.history.append(state.state)
                if path==root: self.fault_hook('after_parent')
                self.fault_hook('after_delete')
                self.fault_hook('before_checkpoint')
                self.fault_hook('after_checkpoint')
                if state.state in ('COMPLETED','PARTIALLY_DELETED'): self.fault_hook('before_ack')
            state.inflight=False; state.writes+=2
            self.storage['audit'].append((plan.plan_id,'BATCH_FINISHED',None))
            return deepcopy(state)

    def pause(self, plan):
        with self.repo.lock:
            state=self.storage['runs'].get(plan.plan_id)
            if state is None or self.storage['plans'].get(plan.plan_id)!=plan: raise ValueError('unknown execution')
            if state.state in ('COMPLETED','PARTIALLY_DELETED'): return deepcopy(state)
            if state.state=='PAUSED': return deepcopy(state)
            if state.inflight: raise ValueError('recover interrupted batch first')
            if state.writes+2>plan.max_writes or state.reads+1>plan.max_reads: raise ValueError('quota prevents pause checkpoint')
            state.state='PAUSED'; state.last_error='OPERATOR_PAUSED'; state.writes+=2; state.reads+=1; state.history.append('PAUSED')
            self.storage['audit'].append((plan.plan_id,'PAUSED',None))
            return deepcopy(state)

    def resume(self, plan):
        with self.repo.lock:
            state=self.storage['runs'].get(plan.plan_id)
            if state is None or self.storage['plans'].get(plan.plan_id)!=plan: raise ValueError('unknown execution')
            if state.state!='PAUSED' or state.last_error!='OPERATOR_PAUSED': return deepcopy(state)
            if state.writes+2>plan.max_writes or state.reads+1>plan.max_reads: raise ValueError('quota prevents resume checkpoint')
            state.state='READY' if state.cursor==0 else 'DELETING'; state.last_error=None; state.writes+=2; state.reads+=1; state.history.append(state.state)
            self.storage['audit'].append((plan.plan_id,'RESUMED',None))
            return deepcopy(state)

    def cancel(self, plan):
        with self.repo.lock:
            state=self.storage['runs'].get(plan.plan_id)
            if state is None or self.storage['plans'].get(plan.plan_id)!=plan: raise ValueError('unknown execution')
            if state.state in ('COMPLETED','PARTIALLY_DELETED'): return deepcopy(state)
            if state.inflight: raise ValueError('recover interrupted batch first')
            if state.state=='PAUSED' and state.last_error=='CANCELLED_BY_OPERATOR': return deepcopy(state)
            if state.writes+2>plan.max_writes or state.reads+1>plan.max_reads: raise ValueError('quota prevents cancellation checkpoint')
            state.state='PAUSED'; state.last_error='CANCELLED_BY_OPERATOR'; state.writes+=2; state.reads+=1; state.history.append('PAUSED')
            self.storage['audit'].append((plan.plan_id,'CANCELLED_BY_OPERATOR',None))
            return deepcopy(state)

    def schedule_retry(self, plan, *, error_code):
        if error_code!='TRANSIENT_FAKE_ERROR': raise ValueError('permanent or unknown errors cannot be retried')
        with self.repo.lock:
            state=self.storage['runs'].get(plan.plan_id)
            if state is None or self.storage['plans'].get(plan.plan_id)!=plan: raise ValueError('unknown execution')
            if state.state in ('COMPLETED','PARTIALLY_DELETED','FAILED','BLOCKED','PAUSED'): raise ValueError('retry unavailable')
            if state.writes+2>plan.max_writes or state.reads+1>plan.max_reads: raise ValueError('retry quota exhausted')
            state.reads+=1; state.writes+=2
            if state.retries>=plan.max_retries or self.clock()+timedelta(seconds=2**state.retries)>=datetime.fromisoformat(plan.deadline):
                state.state='FAILED'; state.last_error='RETRY_EXHAUSTED'; state.inflight=False; state.history.append('FAILED')
            else:
                delay=2**state.retries; state.retries+=1; state.retry_after=(self.clock()+timedelta(seconds=delay)).isoformat()
                state.state='RETRY_WAIT'; state.last_error=error_code; state.inflight=False; state.history.append('RETRY_WAIT')
            self.storage['audit'].append((plan.plan_id,state.state,None))
            return deepcopy(state)
