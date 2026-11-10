"""Experimental job contracts. No durable backend or worker is installed."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from app.storage_analysis.planning import Estimate


class State(str, Enum):
    QUEUED = 'QUEUED'
    FETCHING = 'FETCHING'
    NORMALIZING = 'NORMALIZING'
    ANALYZING = 'ANALYZING'
    PERSISTING = 'PERSISTING'
    VALIDATING = 'VALIDATING'
    RETRY_WAIT = 'RETRY_WAIT'
    CANCELLING = 'CANCELLING'
    CANCELLED = 'CANCELLED'
    COMPLETED = 'COMPLETED'
    PARTIAL = 'PARTIAL'
    FAILED = 'FAILED'
    ABORTED = 'ABORTED'


TERMINAL = frozenset({State.CANCELLED, State.COMPLETED, State.PARTIAL, State.FAILED, State.ABORTED})
ACTIVE = frozenset({State.QUEUED, State.FETCHING, State.NORMALIZING, State.ANALYZING, State.PERSISTING, State.VALIDATING})
TRANSITIONS = {
    State.QUEUED: {State.FETCHING}, State.FETCHING: {State.NORMALIZING},
    State.NORMALIZING: {State.ANALYZING}, State.ANALYZING: {State.PERSISTING},
    State.PERSISTING: {State.VALIDATING}, State.VALIDATING: {State.COMPLETED},
    State.CANCELLING: {State.CANCELLED},
}
for _state in ACTIVE:
    TRANSITIONS[_state] = TRANSITIONS.get(_state, set()) | {State.RETRY_WAIT, State.PARTIAL, State.FAILED, State.ABORTED}


class CoordinationError(Exception): pass
class Conflict(CoordinationError): pass
class LeaseLost(CoordinationError): pass
class InvalidTransition(CoordinationError): pass
class BudgetExceeded(CoordinationError): pass
class DeadlineExceeded(CoordinationError): pass


@dataclass(frozen=True)
class Policy:
    max_attempts: int
    max_requests: int
    retry_budget: int
    deadline: datetime
    lease_seconds: int
    heartbeat_seconds: int
    backoff_seconds: int
    estimate: Estimate
    budget: Estimate


@dataclass(frozen=True)
class CompletionEvidence:
    expected_documents: int
    persisted_documents: int
    validated: bool
    manifest_id: str


@dataclass
class Job:
    job_id: str
    idempotency_key: str
    payload_digest: str
    config_version: str
    state: State
    revision: int
    created_at: datetime
    updated_at: datetime
    policy: Policy
    owner: str | None = None
    fencing: int = 0
    expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    attempts: int = 0
    requests_used: int = 0
    retries_used: int = 0
    retry_after: datetime | None = None
    reason_code: str | None = None
    completion_evidence: CompletionEvidence | None = None
    audit: list = field(default_factory=list)


class JobCoordinator(Protocol):
    def create(self, idempotency_key: str, payload: dict, policy: Policy) -> Job: ...
    def get(self, job_id: str) -> Job | None: ...
    def claim(self, job_id: str, owner: str) -> Job: ...
    def heartbeat(self, job_id: str, owner: str, token: int, revision: int) -> Job: ...
    def transition(self, job_id: str, owner: str, token: int, revision: int, target: State, *, evidence: CompletionEvidence | None = None, reason_code: str | None = None) -> Job: ...
    def request_cancel(self, job_id: str, revision: int) -> Job: ...
    def checkpoint(self, job_id: str, owner: str, token: int, revision: int, requests: int = 0) -> Job: ...
    def release(self, job_id: str, owner: str, token: int, revision: int) -> Job: ...
    def check_publish(self, job_id: str, owner: str, token: int, revision: int) -> bool: ...


def legacy_state(state: State) -> str:
    """Explicit projection only; never replaces the existing API contract."""
    mapping = {State.QUEUED: 'QUEUED', State.FETCHING: 'FETCHING',
        State.NORMALIZING: 'NORMALIZING', State.ANALYZING: 'ANALYZING',
        State.PERSISTING: 'PERSISTING', State.COMPLETED: 'COMPLETED',
        State.PARTIAL: 'PARTIAL', State.FAILED: 'FAILED', State.ABORTED: 'ABORTED'}
    if state not in mapping: raise ValueError('new state has no exact legacy equivalent')
    return mapping[state]
