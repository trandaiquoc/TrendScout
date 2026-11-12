"""Storage-independent shared admission and pre-attempt quota reservation.

This component is not wired to public Fetch. Its guarantees depend on a
serializable durable store and a trustworthy, bounded-skew server clock.
"""

import hashlib
import json
import re
import secrets
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping

from app.ports.atomic_state import AtomicStateStore
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)

SOURCE_POOLS = {
    WIKIMEDIA_SOURCE_ID: "wikimedia",
    GOOGLE_SOURCE_ID: "serpapi",
    BING_SOURCE_ID: "serpapi",
    YOUTUBE_SOURCE_ID: "youtube",
}
_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_OWNER = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_JOB_ID = re.compile(r"^[a-f0-9]{32}$")


class AdmissionDenied(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PoolLimit:
    daily_requests: int
    monthly_requests: int
    daily_cost_units: int
    monthly_cost_units: int
    cost_per_attempt: int
    max_inflight: int = 2
    attempt_timeout_seconds: int = 30

    def __post_init__(self):
        values = (self.daily_requests, self.monthly_requests, self.daily_cost_units,
                  self.monthly_cost_units, self.cost_per_attempt, self.max_inflight,
                  self.attempt_timeout_seconds)
        if any(type(v) is not int or not 1 <= v <= 1_000_000 for v in values):
            raise ValueError("pool limits must be bounded positive integers")
        if self.max_inflight > 100 or self.attempt_timeout_seconds > 300:
            raise ValueError("pool concurrency and deadline must be bounded")


@dataclass(frozen=True)
class AdmissionPolicy:
    enabled_sources: frozenset[str]
    pools: Mapping[str, PoolLimit]
    plan_fingerprint: str
    max_starts_per_window: int = 3
    window_seconds: int = 3600
    cooldown_seconds: int = 60
    lease_seconds: int = 60
    max_attempts: int = 3
    max_job_seconds: int = 300
    receipt_seconds: int = 3600
    max_records: int = 512
    max_status_polls: int = 120
    enabled: bool = False

    def __post_init__(self):
        if (type(self.enabled) is not bool or not isinstance(self.enabled_sources, frozenset)
                or not self.enabled_sources or not self.enabled_sources <= SOURCE_POOLS.keys()
                or not re.fullmatch(r"[a-f0-9]{64}", self.plan_fingerprint)
                or set(self.pools) != {SOURCE_POOLS[source] for source in self.enabled_sources}
                or any(not isinstance(value, PoolLimit) for value in self.pools.values())):
            raise ValueError("invalid server-controlled admission policy")
        for value, lower, upper in ((self.max_starts_per_window, 1, 1000),
                                    (self.window_seconds, 1, 86400),
                                    (self.cooldown_seconds, 0, 86400),
                                    (self.lease_seconds, 1, 300),
                                    (self.max_attempts, 1, 10),
                                    (self.max_job_seconds, 1, 3600),
                                    (self.receipt_seconds, 1, 86400),
                                    (self.max_records, 1, 1000),
                                    (self.max_status_polls, 1, 1000)):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("invalid bounded admission limit")
        if self.receipt_seconds < self.max_job_seconds:
            raise ValueError("receipt retention must cover job lifetime")


def _utc(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware server time required")
    return now.astimezone(timezone.utc)


def _parse(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


class PublicAdmission:
    """Atomic coalescing, fencing and conservative shared quota accounting."""

    def __init__(self, store: AtomicStateStore, policy: AdmissionPolicy, *, state_key: str):
        if not callable(getattr(store, "transact", None)):
            raise TypeError("atomic state store required")
        if not isinstance(policy, AdmissionPolicy) or not _OWNER.fullmatch(state_key):
            raise ValueError("validated server policy and state key required")
        self.store, self.policy, self.state_key = store, policy, state_key
        self._pools = MappingProxyType(dict(policy.pools))
        config = asdict(policy)
        config["enabled_sources"] = sorted(policy.enabled_sources)
        config.pop("enabled")
        self.policy_digest = hashlib.sha256(json.dumps(
            config, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")).hexdigest()

    def _transaction(self, fn):
        def mutate(state):
            if not state:
                state = {"schema": 1, "policy_digest": self.policy_digest,
                         "operational_enabled": self.policy.enabled,
                         "jobs": {}, "keys": {}, "starts": [], "quota": {}, "inflight": {}}
            if state.get("schema") != 1:
                raise AdmissionDenied("STATE_VERSION_UNSUPPORTED")
            if state.get("policy_digest") != self.policy_digest:
                raise AdmissionDenied("POLICY_MISMATCH")
            if not self.policy.enabled or state.get("operational_enabled") is not True:
                raise AdmissionDenied("FEATURE_DISABLED")
            return state, fn(state)
        return self.store.transact(self.state_key, mutate)

    def max_attempt_seconds(self, source_id: str) -> int:
        if source_id not in self.policy.enabled_sources:
            raise AdmissionDenied("SOURCE_OR_ATTEMPT_INVALID")
        return self._pools[SOURCE_POOLS[source_id]].attempt_timeout_seconds

    def operationally_enabled(self) -> bool:
        """Read the server-owned kill switch for public capability reporting."""
        if not self.policy.enabled:
            return False
        def operation(state):
            if not state:
                return {}, True
            if (state.get("schema") != 1
                    or state.get("policy_digest") != self.policy_digest):
                return state, False
            return state, state.get("operational_enabled") is True
        return self.store.transact(self.state_key, operation)

    def transact_owned(self, job_id: str, owner: str, fence: int, *, now: datetime, operation):
        """Run an internal pure mutation only while the current job lease is held."""
        now = _utc(now)
        if not callable(operation):
            raise ValueError("checkpoint operation required")
        def op(state):
            self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            return operation(job)
        return self._transaction(op)

    @staticmethod
    def _guard_clock(state, now):
        previous = state.get("last_observed_at")
        if previous is not None and now < _parse(previous):
            raise AdmissionDenied("CLOCK_REGRESSION")
        state["last_observed_at"] = now.isoformat()

    def admit(self, idempotency_key: str, *, now: datetime) -> dict:
        now = _utc(now)
        if not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
            raise ValueError("invalid idempotency key")
        if not self.policy.enabled:
            raise AdmissionDenied("FEATURE_DISABLED")
        digest = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
        new_id = secrets.token_hex(16)

        def op(state):
            self._guard_clock(state, now)
            jobs, keys = state["jobs"], state["keys"]
            # Retain idempotency receipts at least as long as their job can run.
            for key in list(keys):
                if _parse(keys[key]["expires_at"]) <= now:
                    del keys[key]
            referenced = {record["job_id"] for record in keys.values()}
            for job_id in list(jobs):
                job = jobs[job_id]
                if job_id not in referenced and _parse(job["expires_at"]) <= now:
                    del jobs[job_id]
            existing = keys.get(digest)
            if existing:
                job = jobs.get(existing["job_id"], {})
                return {"job_id": existing["job_id"], "result": "REPLAY",
                        "created_at": job.get("created_at")}
            active = next((job for job in jobs.values()
                           if job["state"] in {"QUEUED", "RUNNING"}
                           and _parse(job["expires_at"]) > now
                           and job["plan_fingerprint"] == self.policy.plan_fingerprint), None)
            if len(keys) >= self.policy.max_records:
                raise AdmissionDenied("RECEIPT_CAPACITY")
            if active:
                job_id = active["job_id"]
                result = "JOINED"
            else:
                starts = [value for value in state["starts"]
                          if _parse(value) > now - timedelta(seconds=self.policy.window_seconds)]
                state["starts"] = starts
                if len(starts) >= self.policy.max_starts_per_window:
                    raise AdmissionDenied("RATE_LIMITED")
                if starts and _parse(starts[-1]) + timedelta(seconds=self.policy.cooldown_seconds) > now:
                    raise AdmissionDenied("COOLDOWN")
                if len(jobs) >= self.policy.max_records:
                    raise AdmissionDenied("JOB_CAPACITY")
                job_id, result = new_id, "CREATED"
                jobs[job_id] = {"job_id": job_id, "state": "QUEUED", "owner": None,
                                "fence": 0, "attempts": 0, "lease_until": None,
                                "created_at": now.isoformat(),
                                "available_at": now.isoformat(),
                                "expires_at": (now + timedelta(seconds=self.policy.max_job_seconds)).isoformat(),
                                "plan_fingerprint": self.policy.plan_fingerprint, "status_polls": 0,
                                "receipts": {}}
                starts.append(now.isoformat())
            keys[digest] = {"job_id": job_id,
                            "expires_at": (now + timedelta(seconds=self.policy.receipt_seconds)).isoformat()}
            return {"job_id": job_id, "result": result,
                    "created_at": jobs.get(job_id, {}).get("created_at")}
        return self._transaction(op)

    def read_job(self, job_id: str, *, now: datetime) -> dict | None:
        """Read a job and charge a bounded shared status-poll allowance.

        Reads remain available after the operational kill switch is set so a
        caller can observe already-admitted work. The allowance and clock guard
        remain atomic across all processes sharing the store.
        """
        now = _utc(now)
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            return None

        def operation(state):
            if (not state or state.get("schema") != 1
                    or state.get("policy_digest") != self.policy_digest):
                raise AdmissionDenied("POLICY_MISMATCH")
            self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            if not isinstance(job, dict):
                return state, None
            polls = job.get("status_polls", 0)
            if type(polls) is not int or polls < 0:
                raise AdmissionDenied("JOB_STATE_INVALID")
            if polls >= self.policy.max_status_polls:
                raise AdmissionDenied("STATUS_POLL_LIMIT")
            job["status_polls"] = polls + 1
            return state, deepcopy(job)
        return self.store.transact(self.state_key, operation)

    def claim(self, job_id: str, owner: str, *, now: datetime) -> dict:
        now = _utc(now)
        if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
            raise ValueError("invalid worker identity")

        def op(state):
            self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            return self._claim_job(job, owner, now)
        return self._transaction(op)

    def _claim_job(self, job, owner, now):
        if not job or _parse(job["expires_at"]) <= now or now < _parse(job["created_at"]):
            raise AdmissionDenied("JOB_EXPIRED")
        if job["state"] == "RUNNING" and _parse(job["lease_until"]) > now:
            raise AdmissionDenied("LEASE_BUSY")
        if job["state"] not in {"QUEUED", "RUNNING"}:
            raise AdmissionDenied("JOB_TERMINAL")
        if job["state"] == "QUEUED" and _parse(job["available_at"]) > now:
            raise AdmissionDenied("JOB_DEFERRED")
        if job["attempts"] >= self.policy.max_attempts:
            raise AdmissionDenied("ATTEMPTS_EXHAUSTED")
        job.update(state="RUNNING", owner=owner, fence=job["fence"] + 1,
                   attempts=job["attempts"] + 1,
                   lease_until=min(now + timedelta(seconds=self.policy.lease_seconds),
                                   _parse(job["expires_at"])).isoformat(),
                   updated_at=now.isoformat())
        return {"job_id": job["job_id"], "fence": job["fence"],
                "lease_until": job["lease_until"], "expires_at": job["expires_at"]}

    def claim_next(self, owner: str, *, now: datetime) -> dict | None:
        """Claim one due job or reclaim an expired lease, in the same transaction."""
        now = _utc(now)
        if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
            raise ValueError("invalid worker identity")
        def op(state):
            self._guard_clock(state, now)
            for job in sorted(state["jobs"].values(), key=lambda x: (x["created_at"], x["job_id"])):
                if job["state"] not in {"QUEUED", "RUNNING"}:
                    continue
                if _parse(job["expires_at"]) <= now:
                    job.update(state="EXPIRED", owner=None, lease_until=None)
                    continue
                due = (job["state"] == "QUEUED" and _parse(job["available_at"]) <= now
                       or job["state"] == "RUNNING" and _parse(job["lease_until"]) <= now)
                if due and job["attempts"] >= self.policy.max_attempts:
                    job.update(state="FAILED", owner=None, lease_until=None)
                elif due:
                    return self._claim_job(job, owner, now)
            return None
        return self._transaction(op)

    def defer(self, job_id: str, owner: str, fence: int, *, now: datetime,
              not_before: datetime) -> None:
        """Release a lease with a bounded retry time; never truncate Retry-After."""
        now, not_before = _utc(now), _utc(not_before)
        def op(state):
            self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            if (not job or job["state"] != "RUNNING" or job["owner"] != owner
                    or type(fence) is not int or job["fence"] != fence
                    or _parse(job["lease_until"]) <= now or _parse(job["expires_at"]) <= now):
                raise AdmissionDenied("LEASE_LOST")
            if not now < not_before < _parse(job["expires_at"]):
                raise AdmissionDenied("RETRY_OUTSIDE_DEADLINE")
            job.update(state="QUEUED", owner=None, lease_until=None,
                       available_at=not_before.isoformat(), updated_at=now.isoformat())
        self._transaction(op)

    def heartbeat(self, job_id: str, owner: str, fence: int, *, now: datetime) -> str:
        """Extend only the currently fenced lease, never past the job deadline."""
        now = _utc(now)
        def op(state):
            self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            job["lease_until"] = min(now + timedelta(seconds=self.policy.lease_seconds),
                                     _parse(job["expires_at"])).isoformat()
            job["updated_at"] = now.isoformat()
            return job["lease_until"]
        return self._transaction(op)

    @staticmethod
    def _require_lease(job, owner, fence, now):
        if (not job or job["state"] != "RUNNING" or job["owner"] != owner
                or type(fence) is not int or job["fence"] != fence
                or _parse(job["lease_until"]) <= now or _parse(job["expires_at"]) <= now):
            raise AdmissionDenied("LEASE_LOST")

    def reserve_attempt(self, job_id: str, owner: str, fence: int, *, source_id: str,
                        attempt_id: str, now: datetime) -> str:
        """NEW permits exactly one outbound attempt. REPLAY never permits a resend."""
        now = _utc(now)
        if (source_id not in self.policy.enabled_sources or not isinstance(attempt_id, str)
                or not _KEY.fullmatch(attempt_id)):
            raise AdmissionDenied("SOURCE_OR_ATTEMPT_INVALID")
        pool = SOURCE_POOLS[source_id]
        limit = self._pools[pool]
        receipt_id = hashlib.sha256(attempt_id.encode("ascii")).hexdigest()

        def op(state):
            self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            existing = job["receipts"].get(receipt_id)
            if existing:
                if existing["source_id"] != source_id:
                    raise AdmissionDenied("ATTEMPT_ID_CONFLICT")
                return "REPLAY"
            if len(job["receipts"]) >= self.policy.max_records:
                raise AdmissionDenied("ATTEMPT_CAPACITY")
            day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
            quota = state["quota"]
            # Never delete a newer window: workers can straddle a UTC boundary.
            daily_key, monthly_key = f"{pool}:day:{day}", f"{pool}:month:{month}"
            # Counters are scoped to UTC calendar windows. Once the server clock
            # has advanced, older windows can no longer affect admission; prune
            # only canonical, known-pool keys strictly before the active window.
            # Unknown/future/malformed entries are retained so corrupt state can
            # consume capacity and fail closed rather than being silently erased.
            for key in list(quota):
                parts = key.split(":") if isinstance(key, str) else ()
                if len(parts) != 3 or parts[0] not in self._pools:
                    continue
                _, window_kind, window_value = parts
                try:
                    if window_kind == "day":
                        parsed = date.fromisoformat(window_value)
                        if parsed.isoformat() == window_value and window_value < day:
                            del quota[key]
                    elif window_kind == "month":
                        parsed = date.fromisoformat(f"{window_value}-01")
                        if parsed.strftime("%Y-%m") == window_value and window_value < month:
                            del quota[key]
                except ValueError:
                    continue
            if len(quota) + sum(key not in quota for key in (daily_key, monthly_key)) > self.policy.max_records:
                raise AdmissionDenied("QUOTA_LEDGER_CAPACITY")
            daily = quota.setdefault(daily_key, {"requests": 0, "cost": 0})
            monthly = quota.setdefault(monthly_key, {"requests": 0, "cost": 0})
            if (daily["requests"] >= limit.daily_requests
                    or monthly["requests"] >= limit.monthly_requests
                    or daily["cost"] + limit.cost_per_attempt > limit.daily_cost_units
                    or monthly["cost"] + limit.cost_per_attempt > limit.monthly_cost_units):
                raise AdmissionDenied("QUOTA_EXHAUSTED")
            inflight = state["inflight"]
            for key in list(inflight):
                if _parse(inflight[key]["expires_at"]) <= now:
                    del inflight[key]
            if sum(entry["pool"] == pool for entry in inflight.values()) >= limit.max_inflight:
                raise AdmissionDenied("PROVIDER_CONCURRENCY_LIMIT")
            if len(inflight) >= self.policy.max_records:
                raise AdmissionDenied("INFLIGHT_CAPACITY")
            for bucket in (daily, monthly):
                bucket["requests"] += 1
                bucket["cost"] += limit.cost_per_attempt
            job["receipts"][receipt_id] = {"source_id": source_id, "pool": pool,
                                          "reserved_at": now.isoformat()}
            inflight[f"{job_id}:{receipt_id}"] = {
                "pool": pool, "expires_at": (now + timedelta(
                    seconds=limit.attempt_timeout_seconds)).isoformat()}
            return "NEW"
        return self._transaction(op)

    def settle_attempt(self, job_id: str, owner: str, fence: int, *, attempt_id: str,
                       now: datetime) -> bool:
        """Release a completed attempt's slot; reserved cost is never refunded here."""
        now = _utc(now)
        if not isinstance(attempt_id, str) or not _KEY.fullmatch(attempt_id):
            raise ValueError("invalid attempt identity")
        receipt_id = hashlib.sha256(attempt_id.encode("ascii")).hexdigest()
        def op(state):
            self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            if receipt_id not in job["receipts"]:
                raise AdmissionDenied("ATTEMPT_NOT_FOUND")
            return state["inflight"].pop(f"{job_id}:{receipt_id}", None) is not None
        return self._transaction(op)

    def check_attempt(self, job_id: str, owner: str, fence: int, *, attempt_id: str,
                      source_id: str, now: datetime) -> datetime:
        """Recheck the switch, lease and reservation immediately before outbound I/O."""
        now = _utc(now)
        if not isinstance(attempt_id, str) or not _KEY.fullmatch(attempt_id):
            raise ValueError("invalid attempt identity")
        receipt_id = hashlib.sha256(attempt_id.encode("ascii")).hexdigest()
        def op(state):
            self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            receipt = job["receipts"].get(receipt_id)
            slot = state["inflight"].get(f"{job_id}:{receipt_id}")
            if (not receipt or receipt["source_id"] != source_id or not slot
                    or _parse(slot["expires_at"]) <= now):
                raise AdmissionDenied("ATTEMPT_NOT_ACTIVE")
            return min(_parse(slot["expires_at"]), _parse(job["lease_until"]),
                       _parse(job["expires_at"]))
        return self._transaction(op)

    def finish(self, job_id: str, owner: str, fence: int, *, now: datetime,
               partial: bool = False) -> None:
        now = _utc(now)
        if type(partial) is not bool:
            raise ValueError("partial must be boolean")
        def op(state):
            self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            job.update(state="PARTIAL" if partial else "COMPLETED", owner=None,
                       lease_until=None, updated_at=now.isoformat(), finished_at=now.isoformat())
        self._transaction(op)

    def disable(self) -> None:
        """Persist a fail-closed switch; wiring authorization is a deployment concern."""
        def op(state):
            if not state:
                state = {"schema": 1, "policy_digest": self.policy_digest,
                         "jobs": {}, "keys": {}, "starts": [], "quota": {}, "inflight": {}}
            if state.get("schema") != 1 or state.get("policy_digest") != self.policy_digest:
                raise AdmissionDenied("POLICY_MISMATCH")
            state["operational_enabled"] = False
            return state, None
        self.store.transact(self.state_key, op)
