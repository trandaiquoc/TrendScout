"""Thread-safe fake transactional coordinator. NOT a distributed lock."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import threading

from app.jobs.contracts import (ACTIVE, TERMINAL, TRANSITIONS, BudgetExceeded, CompletionEvidence,
    Conflict, DeadlineExceeded, InvalidTransition, Job, LeaseLost, Policy, State)
from app.storage_analysis.planning import check_budget


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError('UTC-aware time required')
    return value.astimezone(timezone.utc)


def _identity(payload):
    required = {'job_type', 'source', 'scope', 'window', 'config_version', 'occurrence_id', 'tenant_scope'}
    if not isinstance(payload, dict) or not required <= payload.keys() or any(payload[k] is None or payload[k] == '' for k in required):
        raise ValueError('incomplete job identity')
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError('payload must be JSON') from exc
    return hashlib.sha256(encoded).hexdigest()


def _reason(code):
    if code is not None and (not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code)):
        raise ValueError('reason must be a sanitized code')
    return code


class FakeTransactionalStore:
    """Shared in-process state for multiple simulated clients; not durable."""
    def __init__(self):
        self.mutex = threading.RLock()
        self.jobs = {}
        self.keys = {}
        self.occurrences = {}


class FakeCoordinator:
    """Lock-backed atomic mutations in one process; cannot establish cloud safety."""
    def __init__(self, clock, store=None):
        self._clock = clock
        store = store if store is not None else FakeTransactionalStore()
        self._mutex = store.mutex
        self._jobs = store.jobs
        self._keys = store.keys
        self._occurrences = store.occurrences

    def _now(self): return _utc(self._clock())

    def _record(self, job, now, action):
        job.revision += 1
        job.updated_at = now
        job.audit.append({'revision': job.revision, 'state': job.state.value, 'action': action, 'at': now, 'reason_code': job.reason_code})

    def _owned(self, job, owner, token, revision, now):
        if job.owner != owner or job.fencing != token or job.expires_at is None or job.expires_at <= now:
            raise LeaseLost('lease is no longer valid')
        if job.revision != revision:
            raise Conflict('revision conflict')
        if now >= job.policy.deadline:
            raise DeadlineExceeded('deadline exceeded')

    def create(self, idempotency_key, payload, policy):
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ValueError('invalid idempotency key')
        identity_digest = _identity(payload)
        if not isinstance(policy, Policy): raise ValueError('policy required')
        now = self._now()
        _utc(policy.deadline)
        ints = (policy.max_attempts, policy.max_requests, policy.retry_budget, policy.lease_seconds, policy.heartbeat_seconds, policy.backoff_seconds)
        if any(type(x) is not int for x in ints) or policy.max_attempts < 1 or policy.max_requests < 0 or policy.retry_budget < 0 or not 0 < policy.heartbeat_seconds < policy.lease_seconds or policy.backoff_seconds < 0:
            raise ValueError('invalid policy')
        # Policy is part of the immutable request: same key + changed budget is a conflict.
        policy_json = json.dumps(asdict(policy), sort_keys=True, default=str, separators=(',', ':'))
        digest = hashlib.sha256((identity_digest + policy_json).encode()).hexdigest()
        with self._mutex:
            if idempotency_key in self._keys:
                job = self._jobs[self._keys[idempotency_key]]
                if job.payload_digest != digest: raise Conflict('idempotency key reused with different payload')
                return deepcopy(job)
            if identity_digest in self._occurrences:
                job = self._jobs[self._occurrences[identity_digest]]
                if job.payload_digest != digest: raise Conflict('occurrence reused with different policy')
                self._keys[idempotency_key] = job.job_id
                return deepcopy(job)
            if now >= policy.deadline: raise DeadlineExceeded('deadline exceeded')
            if not check_budget(policy.estimate, policy.budget)['allowed']: raise BudgetExceeded('estimate exceeds budget')
            job_id = hashlib.sha256(idempotency_key.encode()).hexdigest()
            job = Job(job_id, idempotency_key, digest, payload['config_version'], State.QUEUED, 0, now, now, policy)
            job.identity=deepcopy(payload)
            self._record(job, now, 'CREATED')
            self._jobs[job_id] = job
            self._keys[idempotency_key] = job_id
            self._occurrences[identity_digest] = job_id
            return deepcopy(job)

    def get(self, job_id):
        with self._mutex: return deepcopy(self._jobs.get(job_id))

    def claim(self, job_id, owner):
        if not isinstance(owner, str) or not owner.strip(): raise ValueError('owner required')
        with self._mutex:
            now = self._now(); job = self._jobs[job_id]
            if now >= job.policy.deadline: raise DeadlineExceeded('deadline exceeded')
            if job.state in TERMINAL or job.state == State.CANCELLING: raise InvalidTransition('job cannot be claimed')
            if job.owner is not None and job.expires_at > now: raise Conflict('lease held')
            if job.retry_after is not None and now < job.retry_after: raise Conflict('retry backoff')
            if job.attempts >= job.policy.max_attempts: raise BudgetExceeded('attempts exhausted')
            job.attempts += 1; job.fencing += 1; job.owner = owner
            job.expires_at = min(now + timedelta(seconds=job.policy.lease_seconds), job.policy.deadline)
            job.heartbeat_at = now; job.state = State.QUEUED; job.retry_after = None
            self._record(job, now, 'CLAIMED')
            return deepcopy(job)

    def heartbeat(self, job_id, owner, token, revision):
        with self._mutex:
            now = self._now(); job = self._jobs[job_id]
            self._owned(job, owner, token, revision, now)
            if job.state in TERMINAL: raise InvalidTransition('terminal')
            job.heartbeat_at = now; job.expires_at = min(now + timedelta(seconds=job.policy.lease_seconds), job.policy.deadline)
            self._record(job, now, 'HEARTBEAT'); return deepcopy(job)

    def transition(self, job_id, owner, token, revision, target, *, evidence=None, reason_code=None):
        with self._mutex:
            now = self._now(); job = self._jobs[job_id]; self._owned(job, owner, token, revision, now)
            if not isinstance(target, State) or target not in TRANSITIONS.get(job.state, set()): raise InvalidTransition('invalid transition')
            _reason(reason_code)
            if target in (State.RETRY_WAIT, State.PARTIAL, State.FAILED, State.ABORTED) and not reason_code: raise ValueError('reason required')
            if target == State.COMPLETED:
                if not isinstance(evidence, CompletionEvidence) or type(evidence.expected_documents) is not int or evidence.expected_documents <= 0 or type(evidence.persisted_documents) is not int or evidence.persisted_documents != evidence.expected_documents or evidence.validated is not True or not isinstance(evidence.manifest_id, str) or not evidence.manifest_id.strip():
                    raise InvalidTransition('completion evidence incomplete')
                job.completion_evidence = evidence
            if target == State.RETRY_WAIT:
                if job.retries_used >= job.policy.retry_budget or job.attempts >= job.policy.max_attempts: raise BudgetExceeded('retry budget exhausted')
                job.retries_used += 1; job.retry_after = now + timedelta(seconds=job.policy.backoff_seconds)
                job.owner = None; job.expires_at = None
            job.state = target; job.reason_code = reason_code
            self._record(job, now, 'TRANSITION'); return deepcopy(job)

    def request_cancel(self, job_id, revision):
        with self._mutex:
            now = self._now(); job = self._jobs[job_id]
            if job.revision != revision: raise Conflict('revision conflict')
            if job.state in TERMINAL: raise InvalidTransition('terminal')
            # No worker to acknowledge, including expired owner.
            if job.owner is None or job.expires_at <= now:
                job.state = State.CANCELLED; job.owner = None; job.expires_at = None
            else: job.state = State.CANCELLING
            job.reason_code = 'CANCEL_REQUESTED'; self._record(job, now, 'CANCEL_REQUESTED'); return deepcopy(job)

    def checkpoint(self, job_id, owner, token, revision, requests=0):
        with self._mutex:
            now = self._now(); job = self._jobs[job_id]; self._owned(job, owner, token, revision, now)
            if type(requests) is not int or requests < 0: raise ValueError('invalid request count')
            if job.state == State.CANCELLING: return deepcopy(job)
            if job.state not in ACTIVE: raise InvalidTransition('checkpoint outside running job')
            if job.requests_used + requests > job.policy.max_requests: raise BudgetExceeded('request budget exceeded')
            job.requests_used += requests; self._record(job, now, 'CHECKPOINT'); return deepcopy(job)

    def release(self, job_id, owner, token, revision):
        with self._mutex:
            now = self._now(); job = self._jobs[job_id]; self._owned(job, owner, token, revision, now)
            if job.state not in TERMINAL: raise InvalidTransition('release requires terminal state')
            job.owner = None; job.expires_at = None; self._record(job, now, 'RELEASED'); return deepcopy(job)

    def check_publish(self, job_id, owner, token, revision):
        """Preflight only: actual publish must atomically recheck fence; not implemented."""
        with self._mutex:
            now = self._now(); job = self._jobs[job_id]; self._owned(job, owner, token, revision, now)
            if job.state != State.VALIDATING: raise InvalidTransition('publish preflight requires validating')
            return True
