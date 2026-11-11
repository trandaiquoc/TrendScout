"""Firestore transaction-backed coordinator for explicit emulator/test wiring.

This adapter is deliberately not imported by the application composition root.
All mutation callbacks are deterministic, perform reads before writes, and may
be retried by the Firestore SDK. It is not evidence of production safety until
the integration suite has run against Firestore Emulator.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Callable

from google.api_core import exceptions as google_exceptions
from google.cloud import firestore

from app.datasets.evidence import validate_source_evidence
from app.datasets.manifest import validate_manifest
from app.jobs.contracts import (
    ACTIVE,
    TERMINAL,
    TRANSITIONS,
    BudgetExceeded,
    CompletionEvidence,
    Conflict,
    DeadlineExceeded,
    InvalidTransition,
    Job,
    LeaseLost,
    Policy,
    State,
)
from app.storage_analysis.planning import Estimate, check_budget
from app.storage.firestore_emulator import GuardedFirestoreEmulatorClient, SYNTHETIC_PROJECT_ID
from app.workers.safety import ErrorCode, OfflineBudget, RefreshError


# Leave ample room below Firestore's 1 MiB document limit for Firestore's
# encoded field/type overhead. Callers must partition larger payloads before
# starting a write; the emulator does not reliably enforce production limits.
MAX_SHARD_DOCUMENT_JSON_BYTES = 900 * 1024


class TransactionOutcomeUnknown(RuntimeError):
    """A commit RPC failed after the callback completed; inspect durable receipt."""


class TransactionAborted(RuntimeError):
    """The Firestore SDK exhausted its bounded Aborted transaction retries."""


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_shard_payload(data: dict[str, Any]) -> None:
    if type(data) is not dict:
        raise ValueError("invalid shard payload")
    try:
        # Include wrapper keys and digest in the conservative preflight size.
        serialized = json.dumps(
            {"payload": data, "digest": _digest(data), "created_at": "2000-01-01T00:00:00Z"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("shard payload must contain finite JSON-compatible values") from exc
    if len(serialized) > MAX_SHARD_DOCUMENT_JSON_BYTES:
        raise ValueError(
            f"shard document exceeds conservative {MAX_SHARD_DOCUMENT_JSON_BYTES}-byte JSON limit; partition it"
        )


def _doc_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("UTC-aware time required")
    return value.astimezone(timezone.utc)


def _estimate(value: Estimate) -> dict[str, int]:
    return asdict(value)


def _budget(value: OfflineBudget | None) -> dict[str, int] | None:
    return asdict(value) if value is not None else None


def _policy_record(value: Policy) -> dict[str, Any]:
    return {
        "max_attempts": value.max_attempts,
        "max_requests": value.max_requests,
        "retry_budget": value.retry_budget,
        "deadline": value.deadline,
        "lease_seconds": value.lease_seconds,
        "heartbeat_seconds": value.heartbeat_seconds,
        "backoff_seconds": value.backoff_seconds,
        "estimate": _estimate(value.estimate),
        "budget": _estimate(value.budget),
        "operation_budget": _budget(value.operation_budget),
    }


def _policy_from(record: dict[str, Any]) -> Policy:
    budget = record.get("operation_budget")
    return Policy(
        max_attempts=record["max_attempts"],
        max_requests=record["max_requests"],
        retry_budget=record["retry_budget"],
        deadline=record["deadline"],
        lease_seconds=record["lease_seconds"],
        heartbeat_seconds=record["heartbeat_seconds"],
        backoff_seconds=record["backoff_seconds"],
        estimate=Estimate(**record["estimate"]),
        budget=Estimate(**record["budget"]),
        operation_budget=OfflineBudget(**budget) if budget is not None else None,
    )


def _job_record(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "idempotency_key": job.idempotency_key,
        "payload_digest": job.payload_digest,
        "config_version": job.config_version,
        "state": job.state.value,
        "revision": job.revision,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "policy": _policy_record(job.policy),
        "owner": job.owner,
        "fencing": job.fencing,
        "expires_at": job.expires_at,
        "heartbeat_at": job.heartbeat_at,
        "attempts": job.attempts,
        "requests_used": job.requests_used,
        "retries_used": job.retries_used,
        "retry_after": job.retry_after,
        "reason_code": job.reason_code,
        "completion_evidence": asdict(job.completion_evidence) if job.completion_evidence else None,
        "refresh_budget": _budget(job.refresh_budget) if isinstance(job.refresh_budget, OfflineBudget) else None,
        "recovery_reserved": job.recovery_reserved,
        "identity": job.identity,
        "audit": job.audit,
    }


def _job_from(record: dict[str, Any]) -> Job:
    evidence = record.get("completion_evidence")
    refresh_budget = record.get("refresh_budget")
    return Job(
        job_id=record["job_id"],
        idempotency_key=record["idempotency_key"],
        payload_digest=record["payload_digest"],
        config_version=record["config_version"],
        state=State(record["state"]),
        revision=record["revision"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        policy=_policy_from(record["policy"]),
        owner=record.get("owner"),
        fencing=record.get("fencing", 0),
        expires_at=record.get("expires_at"),
        heartbeat_at=record.get("heartbeat_at"),
        attempts=record.get("attempts", 0),
        requests_used=record.get("requests_used", 0),
        retries_used=record.get("retries_used", 0),
        retry_after=record.get("retry_after"),
        reason_code=record.get("reason_code"),
        completion_evidence=CompletionEvidence(**evidence) if evidence else None,
        refresh_budget=OfflineBudget(**refresh_budget) if refresh_budget else None,
        recovery_reserved=record.get("recovery_reserved", False),
        identity=record.get("identity", {}),
        audit=record.get("audit", []),
    )


def _identity(payload: dict[str, Any]) -> str:
    required = {"job_type", "source", "scope", "window", "config_version", "occurrence_id", "tenant_scope"}
    if not isinstance(payload, dict) or not required <= payload.keys() or any(payload[k] in (None, "") for k in required):
        raise ValueError("incomplete job identity")
    if not isinstance(payload["occurrence_id"], str) or not payload["occurrence_id"].strip():
        raise ValueError("occurrence identity required")
    scope = payload.get("scope")
    if not isinstance(scope, dict) or any(not isinstance(scope.get(k), str) or not scope[k].strip() for k in ("country", "access", "project")):
        raise ValueError("country/access/project scope required")
    if not isinstance(payload.get("resource_id"), str) or not payload["resource_id"].strip():
        raise ValueError("resource identity required")
    config = payload.get("dataset_config")
    config_keys = ("analysis_date", "window_start", "window_end", "source_version", "processing_version", "cleaning_version", "classification_version", "algorithm_version", "provenance")
    if not isinstance(config, dict) or any(not isinstance(config.get(k), str) or not config[k].strip() for k in config_keys):
        raise ValueError("full dataset identity required")
    if ("schedule_id" in payload) != ("schedule_version" in payload) or any(not isinstance(payload.get(k), str) or not payload[k].strip() for k in ("schedule_id", "schedule_version") if k in payload):
        raise ValueError("incomplete schedule identity")
    return _digest(payload)


def _reason(code: str | None) -> str | None:
    if code is not None and (not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None):
        raise ValueError("reason must be a sanitized code")
    return code


def _snapshot(transaction: Any, reference: Any) -> Any:
    return next(iter(transaction.get(reference)))


class _Transactional:
    def __init__(self, client: Any, *, clock: Callable[[], datetime] | None = None, max_attempts: int = 5):
        if not isinstance(client, GuardedFirestoreEmulatorClient) or client.project_id != SYNTHETIC_PROJECT_ID:
            raise ValueError("transactional adapter requires the guarded synthetic Firestore Emulator client")
        if not callable(getattr(client, "transaction", None)):
            raise TypeError("Firestore client with transaction() required")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("bounded transaction attempts required")
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_attempts = max_attempts
        self._jobs = client.collection("_ts3_jobs")
        self._keys = client.collection("_ts3_idempotency")
        self._occurrences = client.collection("_ts3_occurrences")
        self._resources = client.collection("_ts3_resources")
        self._datasets = client.collection("_ts3_datasets")
        self._publications = client.collection("_ts3_publications")

    def _now(self) -> datetime:
        return _utc(self.clock())

    def _run(self, callback: Callable[[Any], Any]) -> Any:
        """Run an SDK transaction; never add application side effects to callback."""
        transaction = self.client.transaction(max_attempts=self.max_attempts)
        reached_commit = [False]

        def marked(tx: Any) -> Any:
            reached_commit[0] = False
            result = callback(tx)
            reached_commit[0] = True
            return result

        try:
            return firestore.transactional(marked)(transaction)
        except ValueError as exc:
            if isinstance(exc.__cause__, google_exceptions.Aborted):
                raise TransactionAborted("bounded transaction retries exhausted") from exc.__cause__
            raise
        except (google_exceptions.DeadlineExceeded, google_exceptions.ServiceUnavailable,
                google_exceptions.InternalServerError, google_exceptions.Unknown) as exc:
            if reached_commit[0]:
                raise TransactionOutcomeUnknown("transaction commit outcome is unknown; read durable identity/receipt") from exc
            raise

    @staticmethod
    def _read_job(transaction: Any, reference: Any) -> Job:
        snapshot = _snapshot(transaction, reference)
        if not snapshot.exists:
            raise KeyError("job not found")
        return _job_from(snapshot.to_dict())

    def _owned(self, job: Job, owner: str, token: int, revision: int, now: datetime) -> None:
        if job.owner != owner or job.fencing != token or job.expires_at is None or job.expires_at <= now:
            raise LeaseLost("lease is no longer valid")
        if job.revision != revision:
            raise Conflict("revision conflict")
        if now >= job.policy.deadline:
            raise DeadlineExceeded("deadline exceeded")

    def _charge(self, job: Job, *, reads: int, writes: int, kind: str = "metadata") -> None:
        if not isinstance(job.refresh_budget, OfflineBudget):
            raise ValueError("job operation budget required")
        # Hold one read/write outside normal work until stale-job recovery.
        reserve = 1 if job.recovery_reserved else 0
        if (job.refresh_budget.reads + reads > job.refresh_budget.max_reads - reserve
                or job.refresh_budget.writes + writes > job.refresh_budget.max_writes - reserve):
            raise RefreshError(ErrorCode.QUOTA_EXCEEDED)
        job.refresh_budget.charge(reads=reads, writes=writes, kind=kind)

    @staticmethod
    def _record(job: Job, now: datetime, action: str) -> None:
        job.revision += 1
        job.updated_at = now
        job.audit = (list(job.audit) + [{"revision": job.revision, "state": job.state.value,
                                         "action": action, "at": now, "reason_code": job.reason_code}])[-128:]


class FirestoreTransactionalCoordinator(_Transactional):
    """Implements JobCoordinator with durable occurrence/idempotency transactions."""

    def create(self, idempotency_key: str, payload: dict[str, Any], policy: Policy) -> Job:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ValueError("invalid idempotency key")
        identity_digest = _identity(payload)
        if not isinstance(policy, Policy) or not isinstance(policy.operation_budget, OfflineBudget):
            raise ValueError("fresh operation budget policy required")
        budget = policy.operation_budget
        if any((budget.reads, budget.writes, budget.metadata_operations, budget.heartbeats,
                budget.source_requests, budget.retry_operations)):
            raise ValueError("fresh operation budget policy required")
        if budget.max_reads < 4 or budget.max_writes < 5:
            raise BudgetExceeded("creation budget must cover create, one claim, and reserved stale-job recovery")
        policy_data = _policy_record(policy)
        policy_json = json.dumps(policy_data, sort_keys=True, default=str, separators=(",", ":"))
        request_digest = hashlib.sha256((identity_digest + policy_json).encode()).hexdigest()
        key_ref = self._keys.document(_doc_id(idempotency_key))
        occurrence_ref = self._occurrences.document(_doc_id(payload["occurrence_id"]))
        job_id = hashlib.sha256(idempotency_key.encode()).hexdigest()
        job_ref = self._jobs.document(job_id)
        deadline = _utc(policy.deadline)
        policy_values = (policy.max_attempts, policy.max_requests, policy.retry_budget,
                         policy.lease_seconds, policy.heartbeat_seconds, policy.backoff_seconds)
        if (any(type(value) is not int for value in policy_values)
                or policy.max_attempts < 1 or policy.max_requests < 0 or policy.retry_budget < 0
                or not 0 < policy.heartbeat_seconds < policy.lease_seconds or policy.backoff_seconds < 0):
            raise ValueError("invalid policy")
        if not check_budget(policy.estimate, policy.budget)["allowed"]:
            raise BudgetExceeded("estimate exceeds budget")

        def operation(tx: Any) -> Job:
            now = self._now()
            key = _snapshot(tx, key_ref)
            occurrence = _snapshot(tx, occurrence_ref)
            existing_id = None
            if key.exists:
                entry = key.to_dict()
                if entry.get("payload_digest") != request_digest:
                    raise Conflict("idempotency key reused with different payload or policy")
                existing_id = entry.get("job_id")
            if occurrence.exists:
                entry = occurrence.to_dict()
                if entry.get("payload_digest") != request_digest:
                    raise Conflict("occurrence reused with different payload or policy")
                if existing_id is not None and existing_id != entry.get("job_id"):
                    raise Conflict("idempotency record disagrees with occurrence")
                existing_id = entry.get("job_id")
            if existing_id is not None:
                existing_ref = self._jobs.document(existing_id)
                existing_snapshot = _snapshot(tx, existing_ref)
                if not existing_snapshot.exists:
                    raise Conflict("idempotency index points to missing job")
                job = _job_from(existing_snapshot.to_dict())
                missing_indexes = int(not key.exists) + int(not occurrence.exists)
                self._charge(job, reads=3, writes=1 + missing_indexes)
                tx.set(existing_ref, _job_record(job))
                if not key.exists:
                    tx.create(key_ref, {"job_id": existing_id, "payload_digest": request_digest})
                if not occurrence.exists:
                    tx.create(occurrence_ref, {"job_id": existing_id, "payload_digest": request_digest})
                return deepcopy(job)

            if now >= deadline:
                raise DeadlineExceeded("deadline exceeded")
            initial_budget = deepcopy(policy.operation_budget)
            initial_budget.charge(reads=2, writes=3, kind="metadata")
            job = Job(job_id, idempotency_key, request_digest, payload["config_version"], State.QUEUED,
                      0, now, now, policy)
            job.refresh_budget = initial_budget
            job.recovery_reserved = True
            job.identity = deepcopy(payload)
            self._record(job, now, "CREATED")
            tx.create(job_ref, _job_record(job))
            tx.create(key_ref, {"job_id": job_id, "payload_digest": request_digest})
            tx.create(occurrence_ref, {"job_id": job_id, "payload_digest": request_digest})
            return deepcopy(job)

        return self._run(operation)

    def get(self, job_id: str) -> Job | None:
        reference = self._jobs.document(job_id)
        def operation(tx: Any) -> Job | None:
            snapshot = _snapshot(tx, reference)
            if not snapshot.exists:
                return None
            job = _job_from(snapshot.to_dict())
            self._charge(job, reads=1, writes=1)
            tx.set(reference, _job_record(job))
            return deepcopy(job)
        return self._run(operation)

    def get_owned(self, job_id: str, owner: str, token: int) -> Job:
        reference = self._jobs.document(job_id)
        def operation(tx: Any) -> Job:
            now = self._now()
            job = self._read_job(tx, reference)
            self._owned(job, owner, token, job.revision, now)
            self._charge(job, reads=1, writes=1)
            tx.set(reference, _job_record(job))
            return deepcopy(job)
        return self._run(operation)

    def claim(self, job_id: str, owner: str) -> Job:
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 256:
            raise ValueError("owner required")
        reference = self._jobs.document(job_id)
        def operation(tx: Any) -> Job:
            now = self._now()
            job = self._read_job(tx, reference)
            if now >= job.policy.deadline:
                raise DeadlineExceeded("deadline exceeded")
            if job.state in TERMINAL or job.state == State.CANCELLING:
                raise InvalidTransition("job cannot be claimed")
            if job.owner is not None and job.expires_at is not None and job.expires_at > now:
                raise Conflict("lease held")
            if job.retry_after is not None and now < job.retry_after:
                raise Conflict("retry backoff")
            if job.attempts >= job.policy.max_attempts:
                raise BudgetExceeded("attempts exhausted")
            self._charge(job, reads=1, writes=1)
            job.attempts += 1
            job.fencing += 1
            job.owner = owner
            job.expires_at = min(now + timedelta(seconds=job.policy.lease_seconds), job.policy.deadline)
            job.heartbeat_at = now
            job.state = State.QUEUED
            job.retry_after = None
            self._record(job, now, "CLAIMED")
            tx.set(reference, _job_record(job))
            return deepcopy(job)
        return self._run(operation)

    def abort_stale(self, job_id: str) -> Job:
        reference = self._jobs.document(job_id)
        def operation(tx: Any) -> Job:
            now = self._now()
            job = self._read_job(tx, reference)
            if job.state in TERMINAL or not job.recovery_reserved:
                raise InvalidTransition("recovery unavailable")
            if job.owner is not None and job.expires_at is not None and job.expires_at > now:
                raise Conflict("lease held")
            if job.state == State.RETRY_WAIT and now < job.policy.deadline:
                raise Conflict("retry pending")
            if not isinstance(job.refresh_budget, OfflineBudget) or not job.recovery_reserved:
                raise InvalidTransition("recovery budget unavailable")
            # Creation reserves one read and one write outside the normal
            # execution cap so quota exhaustion cannot strand an expired job.
            job.refresh_budget.charge(reads=1, writes=1, kind="metadata")
            job.state = State.ABORTED
            job.reason_code = "STALE_LEASE_RECOVERY"
            job.owner = None
            job.expires_at = None
            job.fencing += 1
            job.recovery_reserved = False
            self._record(job, now, "STALE_ABORT")
            tx.set(reference, _job_record(job))
            return deepcopy(job)
        return self._run(operation)

    def heartbeat(self, job_id: str, owner: str, token: int, revision: int) -> Job:
        reference = self._jobs.document(job_id)
        def operation(tx: Any) -> Job:
            now = self._now()
            job = self._read_job(tx, reference)
            self._owned(job, owner, token, revision, now)
            if job.state in TERMINAL:
                raise InvalidTransition("terminal")
            self._charge(job, reads=1, writes=1, kind="heartbeat")
            job.heartbeat_at = now
            job.expires_at = min(now + timedelta(seconds=job.policy.lease_seconds), job.policy.deadline)
            self._record(job, now, "HEARTBEAT")
            tx.set(reference, _job_record(job))
            return deepcopy(job)
        return self._run(operation)

    def transition(self, job_id: str, owner: str, token: int, revision: int, target: State,
                   *, evidence: CompletionEvidence | None = None, reason_code: str | None = None) -> Job:
        reference = self._jobs.document(job_id)
        def operation(tx: Any) -> Job:
            now = self._now()
            job = self._read_job(tx, reference)
            self._owned(job, owner, token, revision, now)
            if not isinstance(target, State) or target not in TRANSITIONS.get(job.state, set()):
                raise InvalidTransition("invalid transition")
            _reason(reason_code)
            if target in (State.RETRY_WAIT, State.PARTIAL, State.FAILED, State.ABORTED) and not reason_code:
                raise ValueError("reason required")
            if target == State.COMPLETED:
                raise InvalidTransition("completion requires atomic dataset publication")
            if target == State.RETRY_WAIT:
                if job.retries_used >= job.policy.retry_budget or job.attempts >= job.policy.max_attempts:
                    raise BudgetExceeded("retry budget exhausted")
                job.retries_used += 1
                job.retry_after = now + timedelta(seconds=job.policy.backoff_seconds)
                job.owner = None
                job.expires_at = None
            job.state = target
            job.reason_code = reason_code
            self._charge(job, reads=1, writes=1, kind="retry" if target == State.RETRY_WAIT else "metadata")
            self._record(job, now, "TRANSITION")
            tx.set(reference, _job_record(job))
            return deepcopy(job)
        return self._run(operation)

    def request_cancel(self, job_id: str, revision: int) -> Job:
        reference = self._jobs.document(job_id)
        def operation(tx: Any) -> Job:
            now = self._now()
            job = self._read_job(tx, reference)
            if job.revision != revision:
                raise Conflict("revision conflict")
            if job.state in TERMINAL:
                raise InvalidTransition("terminal")
            self._charge(job, reads=1, writes=1)
            if job.owner is None or job.expires_at is None or job.expires_at <= now:
                job.state = State.CANCELLED
                job.owner = None
                job.expires_at = None
            else:
                job.state = State.CANCELLING
            job.reason_code = "CANCEL_REQUESTED"
            self._record(job, now, "CANCEL_REQUESTED")
            tx.set(reference, _job_record(job))
            return deepcopy(job)
        return self._run(operation)

    def checkpoint(self, job_id: str, owner: str, token: int, revision: int, requests: int = 0) -> Job:
        reference = self._jobs.document(job_id)
        def operation(tx: Any) -> Job:
            now = self._now()
            job = self._read_job(tx, reference)
            self._owned(job, owner, token, revision, now)
            if type(requests) is not int or requests < 0:
                raise ValueError("invalid request count")
            if job.state == State.CANCELLING:
                self._charge(job, reads=1, writes=1)
                tx.set(reference, _job_record(job))
                return deepcopy(job)
            if job.state not in ACTIVE:
                raise InvalidTransition("checkpoint outside running job")
            if job.requests_used + requests > job.policy.max_requests:
                raise BudgetExceeded("request budget exceeded")
            self._charge(job, reads=1, writes=1)
            job.requests_used += requests
            if isinstance(job.refresh_budget, OfflineBudget):
                job.refresh_budget.source_requests += requests
            self._record(job, now, "CHECKPOINT")
            tx.set(reference, _job_record(job))
            return deepcopy(job)
        return self._run(operation)

    def release(self, job_id: str, owner: str, token: int, revision: int) -> Job:
        reference = self._jobs.document(job_id)
        def operation(tx: Any) -> Job:
            now = self._now()
            job = self._read_job(tx, reference)
            self._owned(job, owner, token, revision, now)
            if job.state not in TERMINAL:
                raise InvalidTransition("release requires terminal state")
            self._charge(job, reads=1, writes=1)
            job.owner = None
            job.expires_at = None
            self._record(job, now, "RELEASED")
            tx.set(reference, _job_record(job))
            return deepcopy(job)
        return self._run(operation)

    def check_publish(self, job_id: str, owner: str, token: int, revision: int) -> bool:
        reference = self._jobs.document(job_id)
        def operation(tx: Any) -> bool:
            now = self._now()
            job = self._read_job(tx, reference)
            self._owned(job, owner, token, revision, now)
            if job.state != State.VALIDATING:
                raise InvalidTransition("publish preflight requires validating")
            self._charge(job, reads=1, writes=1)
            tx.set(reference, _job_record(job))
            return True
        return self._run(operation)


class FirestoreTransactionalDatasetStore(_Transactional):
    """Firestore dataset staging and atomic pointer/receipt publication."""

    def __init__(self, client: Any, *, clock: Callable[[], datetime] | None = None, max_attempts: int = 5):
        super().__init__(client, clock=clock, max_attempts=max_attempts)

    def _dataset_ref(self, dataset_id: str) -> Any:
        if not isinstance(dataset_id, str) or not dataset_id or len(dataset_id) > 1500 or "/" in dataset_id:
            raise ValueError("invalid dataset ID")
        return self._datasets.document(dataset_id)

    def _resource_ref(self, resource_id: str) -> Any:
        return self._resources.document(_doc_id(resource_id))

    def _shard_ref(self, dataset_id: str, shard_id: str) -> Any:
        return self._dataset_ref(dataset_id).collection("shards").document(shard_id)

    def _assert_resource_owner(self, tx: Any, resource: str, job: Job, owner: str,
                               job_token: int, revision: int, resource_token: int, now: datetime) -> tuple[Any, Any]:
        job_ref = self._jobs.document(job.job_id)
        resource_ref = self._resource_ref(resource)
        # The caller has already read the job when it passes this helper; resource is read here.
        resource_snapshot = _snapshot(tx, resource_ref)
        if not resource_snapshot.exists:
            raise LeaseLost("resource lease missing")
        lease = resource_snapshot.to_dict()
        self._owned(job, owner, job_token, revision, now)
        if (lease.get("job_id"), lease.get("owner"), lease.get("fencing")) != (job.job_id, owner, resource_token):
            raise LeaseLost("resource lease lost")
        if lease.get("expires_at") is None or lease["expires_at"] <= now:
            raise LeaseLost("resource lease expired")
        if job.identity.get("resource_id") != resource:
            raise Conflict("resource identity mismatch")
        return resource_ref, lease

    def claim_resource(self, resource: str, job_id: str, owner: str, job_token: int, revision: int) -> int:
        job_ref = self._jobs.document(job_id)
        resource_ref = self._resource_ref(resource)
        def operation(tx: Any) -> int:
            now = self._now()
            job = self._read_job(tx, job_ref)
            self._owned(job, owner, job_token, revision, now)
            resource_snapshot = _snapshot(tx, resource_ref)
            if job.identity.get("resource_id") != resource:
                raise Conflict("resource identity mismatch")
            if job.state not in ACTIVE:
                raise InvalidTransition("job cannot claim resource")
            prior = resource_snapshot.to_dict() if resource_snapshot.exists else {}
            if prior.get("owner") is not None and prior.get("expires_at") is not None and prior["expires_at"] > now:
                raise Conflict("resource leased")
            prior_fence = prior.get("fencing", 0)
            pointer_revision = prior.get("pointer_revision", 0)
            if type(prior_fence) is not int or prior_fence < 0 or type(pointer_revision) is not int or pointer_revision < 0:
                raise Conflict("resource fencing metadata is invalid")
            token = prior_fence + 1
            self._charge(job, reads=2, writes=2)
            updated_job = deepcopy(job)
            self._record(updated_job, now, "RESOURCE_CLAIMED")
            tx.set(job_ref, _job_record(updated_job))
            tx.set(resource_ref, {
                "resource_id": resource,
                "job_id": job_id,
                "owner": owner,
                "fencing": token,
                "expires_at": job.expires_at,
                "active_dataset": prior.get("active_dataset"),
                "pointer_revision": pointer_revision,
                "cleanup_reserved": prior.get("cleanup_reserved", False),
                "tombstone": prior.get("tombstone", False),
            })
            return token
        return self._run(operation)

    def get_owned_resource_lease(self, resource: str, job_id: str, owner: str,
                                 job_token: int) -> dict[str, Any]:
        """Recover a claim whose commit acknowledgement may have been lost."""
        job_ref = self._jobs.document(job_id)
        resource_ref = self._resource_ref(resource)
        def operation(tx: Any) -> dict[str, Any]:
            now = self._now()
            job = self._read_job(tx, job_ref)
            self._owned(job, owner, job_token, job.revision, now)
            resource_snapshot = _snapshot(tx, resource_ref)
            if not resource_snapshot.exists:
                raise LeaseLost("resource lease missing")
            lease = resource_snapshot.to_dict()
            if (lease.get("job_id"), lease.get("owner")) != (job_id, owner):
                raise LeaseLost("resource lease belongs to another owner")
            if lease.get("expires_at") is None or lease["expires_at"] <= now:
                raise LeaseLost("resource lease expired")
            if job.identity.get("resource_id") != resource:
                raise Conflict("resource identity mismatch")
            fencing = lease.get("fencing")
            pointer_revision = lease.get("pointer_revision", 0)
            if type(fencing) is not int or fencing < 1 or type(pointer_revision) is not int or pointer_revision < 0:
                raise Conflict("resource fencing metadata is invalid")
            self._charge(job, reads=2, writes=1)
            tx.set(job_ref, _job_record(job))
            return {"resource_fencing": fencing,
                    "expires_at": lease["expires_at"],
                    "active_dataset": lease.get("active_dataset"),
                    "pointer_revision": lease.get("pointer_revision", 0)}
        return self._run(operation)

    def heartbeat_resource(self, resource: str, job_id: str, owner: str, job_token: int,
                           revision: int, resource_token: int) -> Job:
        job_ref = self._jobs.document(job_id)
        resource_ref = self._resource_ref(resource)
        def operation(tx: Any) -> Job:
            now = self._now()
            job = self._read_job(tx, job_ref)
            lease_ref, lease = self._assert_resource_owner(tx, resource, job, owner, job_token, revision, resource_token, now)
            if job.state in TERMINAL:
                raise InvalidTransition("terminal")
            self._charge(job, reads=2, writes=2, kind="heartbeat")
            job.heartbeat_at = now
            job.expires_at = min(now + timedelta(seconds=job.policy.lease_seconds), job.policy.deadline)
            self._record(job, now, "HEARTBEAT")
            lease["expires_at"] = job.expires_at
            tx.set(job_ref, _job_record(job))
            tx.set(lease_ref, lease)
            return deepcopy(job)
        return self._run(operation)

    def retry_wait(self, resource: str, job_id: str, owner: str, job_token: int, revision: int,
                   resource_token: int, reason: str, delay: timedelta | None = None) -> Job:
        reason = _reason(reason)
        if delay is not None and (not isinstance(delay, timedelta) or delay.total_seconds() < 0):
            raise ValueError("invalid retry delay")
        job_ref = self._jobs.document(job_id)
        resource_ref = self._resource_ref(resource)
        def operation(tx: Any) -> Job:
            now = self._now()
            job = self._read_job(tx, job_ref)
            lease_ref, lease = self._assert_resource_owner(tx, resource, job, owner, job_token, revision, resource_token, now)
            if State.RETRY_WAIT not in TRANSITIONS.get(job.state, set()):
                raise InvalidTransition("retry not allowed")
            if job.retries_used >= job.policy.retry_budget or job.attempts >= job.policy.max_attempts:
                raise BudgetExceeded("retry budget exhausted")
            self._charge(job, reads=2, writes=2, kind="retry")
            job.retries_used += 1
            job.retry_after = now + (delay if delay is not None else timedelta(seconds=job.policy.backoff_seconds))
            job.state = State.RETRY_WAIT
            job.reason_code = reason
            job.owner = None
            job.expires_at = None
            self._record(job, now, "RETRY_WAIT")
            lease["owner"] = None
            lease["expires_at"] = now
            tx.set(job_ref, _job_record(job))
            tx.set(lease_ref, lease)
            return deepcopy(job)
        return self._run(operation)

    def draft(self, dataset_id: str, resource: str, job_id: str, owner: str,
              job_token: int, revision: int, resource_token: int, metadata: dict[str, Any]) -> None:
        job_ref = self._jobs.document(job_id)
        resource_ref = self._resource_ref(resource)
        dataset_ref = self._dataset_ref(dataset_id)
        def operation(tx: Any) -> None:
            now = self._now()
            job = self._read_job(tx, job_ref)
            self._assert_resource_owner(tx, resource, job, owner, job_token, revision, resource_token, now)
            dataset_snapshot = _snapshot(tx, dataset_ref)
            if dataset_snapshot.exists:
                prior = dataset_snapshot.to_dict()
                if (prior.get("job_id"), prior.get("resource_id"), prior.get("owner"),
                    prior.get("fencing"), prior.get("metadata")) != (job_id, resource, owner, resource_token, metadata):
                    raise Conflict("draft identity conflict")
                self._charge(job, reads=3, writes=1)
                tx.set(job_ref, _job_record(job))
                return None
            if type(metadata) is not dict or not metadata:
                raise ValueError("dataset metadata required")
            self._charge(job, reads=3, writes=2)
            tx.set(job_ref, _job_record(job))
            tx.create(dataset_ref, {"dataset_id": dataset_id, "resource_id": resource, "job_id": job_id,
                                    "owner": owner, "fencing": resource_token, "metadata": metadata,
                                    "identity": job.identity,
                                    "state": "DRAFT", "created_at": now, "verified": None})
        self._run(operation)

    def put_shard(self, dataset_id: str, resource: str, job_id: str, owner: str,
                  job_token: int, revision: int, resource_token: int, shard_id: str,
                  data: dict[str, Any]) -> None:
        if not isinstance(shard_id, str) or not shard_id.strip() or "/" in shard_id:
            raise ValueError("invalid shard ID")
        if len(shard_id) > 1500:
            raise ValueError("invalid shard payload")
        _validate_shard_payload(data)
        job_ref = self._jobs.document(job_id)
        resource_ref = self._resource_ref(resource)
        dataset_ref = self._dataset_ref(dataset_id)
        shard_ref = self._shard_ref(dataset_id, shard_id)
        def operation(tx: Any) -> None:
            now = self._now()
            job = self._read_job(tx, job_ref)
            self._assert_resource_owner(tx, resource, job, owner, job_token, revision, resource_token, now)
            dataset_snapshot = _snapshot(tx, dataset_ref)
            shard_snapshot = _snapshot(tx, shard_ref)
            if not dataset_snapshot.exists:
                raise KeyError("dataset draft missing")
            dataset = dataset_snapshot.to_dict()
            if (dataset.get("resource_id"), dataset.get("job_id"), dataset.get("owner"),
                dataset.get("fencing"), dataset.get("state")) != (resource, job_id, owner, resource_token, "DRAFT"):
                raise Conflict("immutable or foreign draft")
            if shard_snapshot.exists:
                if shard_snapshot.to_dict().get("payload") != data:
                    raise Conflict("immutable shard conflict")
                self._charge(job, reads=4, writes=1)
                tx.set(job_ref, _job_record(job))
                return None
            self._charge(job, reads=4, writes=2)
            tx.set(job_ref, _job_record(job))
            tx.create(shard_ref, {"payload": data, "digest": _digest(data), "created_at": now})
        self._run(operation)

    def put_quality_report(self, dataset_id: str, resource: str, job_id: str, owner: str,
                           job_token: int, revision: int, resource_token: int,
                           report: dict[str, Any]) -> None:
        if type(report) is not dict:
            raise ValueError("quality report mapping required")
        job_ref = self._jobs.document(job_id)
        dataset_ref = self._dataset_ref(dataset_id)
        quality_ref = dataset_ref.collection("meta").document("quality")
        def operation(tx: Any) -> None:
            now = self._now()
            job = self._read_job(tx, job_ref)
            self._assert_resource_owner(tx, resource, job, owner, job_token, revision, resource_token, now)
            dataset_snapshot = _snapshot(tx, dataset_ref)
            quality_snapshot = _snapshot(tx, quality_ref)
            if not dataset_snapshot.exists:
                raise KeyError("dataset draft missing")
            dataset = dataset_snapshot.to_dict()
            if (dataset.get("resource_id"), dataset.get("job_id"), dataset.get("owner"),
                dataset.get("fencing"), dataset.get("state")) != (resource, job_id, owner, resource_token, "DRAFT"):
                raise Conflict("immutable or foreign report")
            if quality_snapshot.exists:
                if quality_snapshot.to_dict().get("payload") != report:
                    raise Conflict("immutable quality report")
                self._charge(job, reads=4, writes=1)
                tx.set(job_ref, _job_record(job))
                return None
            self._charge(job, reads=4, writes=2)
            tx.set(job_ref, _job_record(job))
            tx.create(quality_ref, {"payload": report, "digest": _digest(report), "created_at": now})
        self._run(operation)

    def seal(self, dataset_id: str, resource: str, job_id: str, owner: str,
             job_token: int, revision: int, resource_token: int, manifest: dict[str, Any]) -> None:
        if not isinstance(manifest, dict) or manifest.get("manifest_version") != "offline-manifest-v1":
            raise ValueError("strict manifest required")
        job_ref = self._jobs.document(job_id)
        dataset_ref = self._dataset_ref(dataset_id)
        manifest_ref = dataset_ref.collection("meta").document("manifest")
        def operation(tx: Any) -> None:
            now = self._now()
            job = self._read_job(tx, job_ref)
            self._assert_resource_owner(tx, resource, job, owner, job_token, revision, resource_token, now)
            dataset_snapshot = _snapshot(tx, dataset_ref)
            manifest_snapshot = _snapshot(tx, manifest_ref)
            if not dataset_snapshot.exists:
                raise KeyError("dataset draft missing")
            dataset = dataset_snapshot.to_dict()
            if (dataset.get("resource_id"), dataset.get("job_id"), dataset.get("owner"),
                dataset.get("fencing"), dataset.get("state")) != (resource, job_id, owner, resource_token, "DRAFT"):
                if dataset.get("state") == "VALIDATING" and manifest_snapshot.exists and manifest_snapshot.to_dict().get("payload") == manifest:
                    self._charge(job, reads=4, writes=1)
                    tx.set(job_ref, _job_record(job))
                    return None
                raise Conflict("cannot seal dataset")
            if manifest_snapshot.exists:
                raise Conflict("manifest already exists")
            self._charge(job, reads=4, writes=3)
            dataset["state"] = "VALIDATING"
            tx.set(job_ref, _job_record(job))
            tx.set(dataset_ref, dataset)
            tx.create(manifest_ref, {"payload": manifest, "digest": _digest(manifest), "created_at": now})
        self._run(operation)

    def verify(self, dataset_id: str, resource: str, job_id: str, owner: str,
               job_token: int, revision: int, resource_token: int, *, quality_ok: Any = None) -> bool:
        """Read immutable sealed shards, independently replay evidence, then mark READY."""
        del quality_ok  # Caller-supplied booleans are never verification evidence.
        dataset_ref = self._dataset_ref(dataset_id)
        initial = dataset_ref.get()
        if not initial.exists:
            raise KeyError("dataset missing")
        dataset = initial.to_dict()
        shards: dict[str, Any] = {}
        for shard_snapshot in dataset_ref.collection("shards").stream():
            shard_doc = shard_snapshot.to_dict()
            payload = shard_doc.get("payload")
            if not isinstance(payload, dict) or shard_doc.get("digest") != _digest(payload):
                return False
            shards[shard_snapshot.id] = payload
        manifest_doc = dataset_ref.collection("meta").document("manifest").get()
        quality_doc = dataset_ref.collection("meta").document("quality").get()
        if not manifest_doc.exists or not quality_doc.exists:
            return False
        manifest = manifest_doc.to_dict().get("payload")
        report = quality_doc.to_dict().get("payload")
        metadata = dataset.get("metadata")
        verified = self._validate_sealed_dataset(dataset, manifest, report, metadata, shards, job_id, resource)
        if not verified:
            return False
        now = self._now()
        job_ref = self._jobs.document(job_id)
        resource_ref = self._resource_ref(resource)
        identity_fingerprint = _digest(dataset.get("identity", {}))
        verification = {
            "manifest_digest": _digest(manifest),
            "quality_digest": _digest(report),
            "metadata_digest": _digest(metadata),
            "shard_ids": sorted(shards),
            "shard_digests": {key: _digest(value) for key, value in sorted(shards.items())},
            "identity_digest": identity_fingerprint,
            "verification_version": "firestore-strict-verifier-v1",
        }
        def operation(tx: Any) -> bool:
            now = self._now()
            job = self._read_job(tx, job_ref)
            if job.state != State.VALIDATING:
                raise InvalidTransition("dataset verification requires validating job")
            _, lease = self._assert_resource_owner(tx, resource, job, owner, job_token, revision, resource_token, now)
            current_dataset_snapshot = _snapshot(tx, dataset_ref)
            current_manifest_snapshot = _snapshot(tx, dataset_ref.collection("meta").document("manifest"))
            current_quality_snapshot = _snapshot(tx, dataset_ref.collection("meta").document("quality"))
            if not current_dataset_snapshot.exists or not current_manifest_snapshot.exists or not current_quality_snapshot.exists:
                return False
            current_dataset = current_dataset_snapshot.to_dict()
            if current_dataset.get("state") != "VALIDATING":
                return False
            if (_digest(current_dataset.get("metadata")) != verification["metadata_digest"]
                    or _digest(current_dataset.get("identity")) != verification["identity_digest"]):
                return False
            if current_manifest_snapshot.to_dict().get("digest") != verification["manifest_digest"]:
                return False
            if current_quality_snapshot.to_dict().get("digest") != verification["quality_digest"]:
                return False
            if lease.get("fencing") != resource_token:
                raise LeaseLost("resource fence changed")
            # Reads include the initial dataset/manifest/quality fetch, every
            # immutable shard document, and the bounded closing transaction.
            self._charge(job, reads=8 + len(shards), writes=2)
            job_updated = deepcopy(job)
            self._record(job_updated, now, "DATASET_READY")
            current_dataset["state"] = "READY"
            current_dataset["verified"] = verification
            tx.set(job_ref, _job_record(job_updated))
            tx.set(dataset_ref, current_dataset)
            return True
        return self._run(operation)

    @staticmethod
    def _validate_sealed_dataset(dataset: dict[str, Any], manifest: Any, report: Any,
                                 metadata: Any, shards: dict[str, Any], job_id: str,
                                 resource: str) -> bool:
        if dataset.get("state") != "VALIDATING" or not isinstance(manifest, dict) or not isinstance(report, dict):
            return False
        if not validate_manifest(manifest, dataset_id=dataset["dataset_id"], job_id=job_id,
                                 resource_id=resource, metadata=metadata, shards=shards, hash_fn=_digest):
            return False
        if manifest.get("quality_report") != report or manifest.get("quality_report_digest") != _digest(report):
            return False
        if report.get("dataset_id") != dataset["dataset_id"]:
            return False
        if report.get("source_id") != manifest.get("source_id") or report.get("processing_version") != manifest.get("processing_version"):
            return False
        snapshots = report.get("snapshots")
        if not isinstance(snapshots, list) or not snapshots:
            return False
        if not any(isinstance(item, dict) and item.get("date") == manifest.get("analysis_date")
                   and item.get("source_url") == manifest.get("provenance") for item in snapshots):
            return False
        for item in snapshots:
            if not isinstance(item, dict):
                return False
            counts = [item.get(key) for key in ("raw_count", "accepted_count", "rejected_count", "duplicate_count")]
            reasons, flags = item.get("rejection_reasons"), item.get("quality_flags")
            if not item.get("date") or not item.get("status") or not item.get("source_url") or not isinstance(reasons, dict) or not isinstance(flags, list):
                return False
            if any(value is None for value in counts):
                if not all(value is None for value in counts):
                    return False
            elif (any(type(value) is not int or value < 0 for value in counts)
                  or counts[0] != sum(counts[1:])
                  or any(type(value) is not int or value < 0 for value in reasons.values())
                  or sum(reasons.values()) != counts[2]):
                return False
        identity = dataset.get("identity")
        if not isinstance(identity, dict):
            return False
        scope = identity.get("scope", {})
        if manifest.get("occurrence_id") != identity.get("occurrence_id") or manifest.get("source_id") != identity.get("source"):
            return False
        if not isinstance(scope, dict) or any(manifest.get(key) != value for key, value in scope.items() if key in ("country", "access", "project")):
            return False
        try:
            from datetime import date
            start = date.fromisoformat(manifest["window_start"])
            end = date.fromisoformat(manifest["window_end"])
            analysis = date.fromisoformat(manifest["analysis_date"])
            if (end != analysis or type(identity.get("window")) is not int
                    or (end - start).days != identity["window"]):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        config_keys = ("analysis_date", "window_start", "window_end", "source_version", "processing_version",
                       "cleaning_version", "classification_version", "algorithm_version", "provenance")
        config = identity.get("dataset_config")
        if not isinstance(config, dict) or any(config.get(key) != manifest.get(key) for key in config_keys):
            return False
        if manifest.get("config_version") != identity.get("config_version"):
            return False
        if any((key in manifest) != (key in identity) or manifest.get(key) != identity.get(key)
               for key in ("schedule_id", "schedule_version")):
            return False
        if manifest.get("source_id") == "wikimedia_pageviews_top_per_country":
            return validate_source_evidence(manifest, shards, report)
        return False

    def active(self, resource: str) -> tuple[str | None, int]:
        snapshot = self._resource_ref(resource).get()
        if not snapshot.exists:
            return None, 0
        value = snapshot.to_dict()
        return value.get("active_dataset"), value.get("pointer_revision", 0)

    def publish(self, dataset_id: str, resource: str, job_id: str, owner: str,
                job_token: int, revision: int, resource_token: int,
                expected_active: str | None, expected_revision: int) -> str:
        job_ref = self._jobs.document(job_id)
        resource_ref = self._resource_ref(resource)
        dataset_ref = self._dataset_ref(dataset_id)
        # One durable receipt per resource/job makes replay addressable even
        # after a later resource fence changes the dataset suffix.
        receipt_id = _doc_id(f"{resource}\0{job_id}")
        receipt_ref = self._publications.document(receipt_id)
        def operation(tx: Any) -> str:
            now = self._now()
            # All reads are complete before any write below.
            job = self._read_job(tx, job_ref)
            resource_snapshot = _snapshot(tx, resource_ref)
            dataset_snapshot = _snapshot(tx, dataset_ref)
            manifest_ref = dataset_ref.collection("meta").document("manifest")
            quality_ref = dataset_ref.collection("meta").document("quality")
            manifest_snapshot = _snapshot(tx, manifest_ref)
            quality_snapshot = _snapshot(tx, quality_ref)
            receipt_snapshot = _snapshot(tx, receipt_ref)
            prior_dataset_ref = None
            prior_dataset_snapshot = None
            if resource_snapshot.exists:
                current_pointer = resource_snapshot.to_dict().get("active_dataset")
                if current_pointer is not None:
                    prior_dataset_ref = self._dataset_ref(current_pointer)
                    prior_dataset_snapshot = _snapshot(tx, prior_dataset_ref)
            if receipt_snapshot.exists:
                receipt = receipt_snapshot.to_dict()
                expected_identity = {"resource_id": resource, "dataset_id": dataset_id, "job_id": job_id,
                                     "owner": owner, "job_fencing": job_token, "resource_fencing": resource_token,
                                     "previous_dataset": expected_active, "previous_revision": expected_revision}
                if (job.state != State.COMPLETED or receipt.get("identity") != expected_identity
                        or receipt.get("identity_digest") != _digest(expected_identity)):
                    raise Conflict("publication receipt identity mismatch")
                self._charge(job, reads=6 + int(prior_dataset_snapshot is not None), writes=1)
                tx.set(job_ref, _job_record(job))
                return "ALREADY_PUBLISHED"
            if not resource_snapshot.exists or not dataset_snapshot.exists or not manifest_snapshot.exists or not quality_snapshot.exists:
                raise Conflict("publication metadata missing")
            lease = resource_snapshot.to_dict()
            dataset = dataset_snapshot.to_dict()
            manifest_doc = manifest_snapshot.to_dict()
            quality_doc = quality_snapshot.to_dict()
            self._owned(job, owner, job_token, revision, now)
            if (lease.get("job_id"), lease.get("owner"), lease.get("fencing")) != (job_id, owner, resource_token):
                raise LeaseLost("resource fence lost")
            if lease.get("expires_at") is None or lease["expires_at"] <= now:
                raise LeaseLost("resource lease expired")
            if job.state != State.VALIDATING or dataset.get("state") != "READY":
                raise Conflict("job or dataset not ready")
            if dataset.get("tombstone") or dataset.get("cleanup_reserved") or dataset.get("state") == "CLEANING":
                raise Conflict("dataset reserved or tombstoned for cleanup")
            if (dataset.get("dataset_id"), dataset.get("resource_id"), dataset.get("job_id"),
                dataset.get("owner"), dataset.get("fencing")) != (dataset_id, resource, job_id, owner, resource_token):
                raise Conflict("dataset identity mismatch")
            manifest = manifest_doc.get("payload")
            report = quality_doc.get("payload")
            verification = dataset.get("verified")
            if not isinstance(manifest, dict) or not isinstance(report, dict):
                raise Conflict("manifest or quality report is invalid")
            if not isinstance(verification, dict) or verification.get("manifest_digest") != _digest(manifest):
                raise Conflict("strict manifest verification missing or stale")
            if verification.get("quality_digest") != _digest(report) or verification.get("metadata_digest") != _digest(dataset.get("metadata")):
                raise Conflict("verified dataset metadata changed")
            if verification.get("identity_digest") != _digest(job.identity):
                raise Conflict("job identity changed after verification")
            if manifest_doc.get("digest") != _digest(manifest) or quality_doc.get("digest") != _digest(report):
                raise Conflict("manifest or quality digest mismatch")
            manifest_shards = manifest.get("shards")
            verified_ids = verification.get("shard_ids")
            verified_digests = verification.get("shard_digests")
            if (not isinstance(manifest_shards, dict) or not isinstance(verified_ids, list)
                    or not isinstance(verified_digests, dict)
                    or verified_ids != sorted(manifest_shards)
                    or verified_digests != {key: value.get("sha256") for key, value in manifest_shards.items()}):
                raise Conflict("verified shard inventory does not match strict manifest")
            if (lease.get("active_dataset"), lease.get("pointer_revision")) != (expected_active, expected_revision):
                raise Conflict("active pointer CAS conflict")
            if expected_active is not None:
                if prior_dataset_snapshot is None or not prior_dataset_snapshot.exists:
                    raise Conflict("current active dataset metadata missing")
                if prior_dataset_snapshot.to_dict().get("state") != "ACTIVE":
                    raise Conflict("current pointer does not reference an active dataset")
            if lease.get("cleanup_reserved") or lease.get("tombstone"):
                raise Conflict("resource reserved for cleanup")
            if expected_active == dataset_id:
                raise Conflict("active pointer already names candidate without matching receipt")
            self._charge(job, reads=7 if prior_dataset_snapshot is not None else 6, writes=5 if prior_dataset_snapshot is not None else 4)
            updated_job = deepcopy(job)
            updated_job.state = State.COMPLETED
            updated_job.reason_code = None
            updated_job.completion_evidence = CompletionEvidence(
                expected_documents=len(verified_ids) + 3,
                persisted_documents=len(verified_ids) + 3,
                validated=True,
                manifest_id=dataset_id,
            )
            self._record(updated_job, now, "PUBLISHED")
            lease["active_dataset"] = dataset_id
            lease["pointer_revision"] = expected_revision + 1
            dataset["state"] = "ACTIVE"
            identity = {"resource_id": resource, "dataset_id": dataset_id, "job_id": job_id,
                        "owner": owner, "job_fencing": job_token, "resource_fencing": resource_token,
                        "previous_dataset": expected_active, "previous_revision": expected_revision}
            tx.set(job_ref, _job_record(updated_job))
            tx.set(resource_ref, lease)
            tx.set(dataset_ref, dataset)
            if prior_dataset_ref is not None and prior_dataset_snapshot is not None and prior_dataset_snapshot.exists:
                prior_dataset = prior_dataset_snapshot.to_dict()
                prior_dataset["state"] = "SUPERSEDED"
                tx.set(prior_dataset_ref, prior_dataset)
            tx.create(receipt_ref, {"identity": identity, "identity_digest": _digest(identity),
                                    "committed_at": now, "pointer_revision": expected_revision + 1})
            return "PUBLISHED"
        return self._run(operation)

    def replay_publication(self, resource: str, job_id: str, owner: str,
                           dataset_id: str, fresh_on_retry: bool,
                           expected_active: str | None, expected_revision: int) -> str:
        """Read the deterministic receipt; never mutate/roll back the active pointer."""
        job_ref = self._jobs.document(job_id)
        def operation(tx: Any) -> str:
            value = self._read_job(tx, job_ref)
            receipt_ref = self._publications.document(_doc_id(f"{resource}\0{job_id}"))
            receipt_snapshot = _snapshot(tx, receipt_ref)
            if not receipt_snapshot.exists:
                raise Conflict("publication receipt missing")
            receipt = receipt_snapshot.to_dict()
            identity = receipt.get("identity", {})
            resource_fence = identity.get("resource_fencing")
            candidate = f"{dataset_id}-f{resource_fence}" if fresh_on_retry else dataset_id
            if (receipt.get("identity_digest") != _digest(identity)
                    or value.state != State.COMPLETED or identity.get("job_id") != job_id
                    or identity.get("owner") != owner or identity.get("resource_id") != resource
                    or identity.get("dataset_id") != candidate
                    or identity.get("previous_dataset") != expected_active
                    or identity.get("previous_revision") != expected_revision):
                raise Conflict("publication receipt mismatch")
            self._charge(value, reads=2, writes=1)
            tx.set(job_ref, _job_record(value))
            return "ALREADY_PUBLISHED"
        return self._run(operation)

    def read_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        snapshot = self._dataset_ref(dataset_id).get()
        if not snapshot.exists:
            return None
        value = snapshot.to_dict()
        result = dict(value)
        result["shards"] = {
            item.id: item.to_dict().get("payload")
            for item in self._dataset_ref(dataset_id).collection("shards").stream()
        }
        manifest = self._dataset_ref(dataset_id).collection("meta").document("manifest").get()
        quality = self._dataset_ref(dataset_id).collection("meta").document("quality").get()
        result["manifest"] = manifest.to_dict().get("payload") if manifest.exists else None
        result["quality_report"] = quality.to_dict().get("payload") if quality.exists else None
        return result
