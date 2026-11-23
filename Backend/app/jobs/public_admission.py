"""Storage-independent shared admission and pre-attempt quota reservation.

The local Google LIVE Fetch can explicitly use it with SQLite or Firestore.
Its guarantees depend on a serializable durable store and a trustworthy,
bounded-skew server clock.
"""

import hashlib
import json
import re
import secrets
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo

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
SOURCE_ALLOCATION_POOLS = {
    GOOGLE_SOURCE_ID: "serpapi_google",
    BING_SOURCE_ID: "serpapi_bing",
}
YOUTUBE_OTHER_POOL = "youtube_other"
YOUTUBE_PROVIDER_TIMEZONE = ZoneInfo("America/Los_Angeles")
_KEY = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_OWNER = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_JOB_ID = re.compile(r"^[a-f0-9]{32}$")
OWNER_FRESH_ACCEPTANCE_MARKER = "D6_FOUR_SOURCE_FRESH_V1"
MANUAL_SOURCE_REFRESH_MARKER = "VERCEL_OPERATOR_AUTHENTICATED_MANUAL_REFRESH_V1"
_CLOCK_REGRESSION_TOLERANCE = timedelta(seconds=5)


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
    source_allocations: Mapping[str, str] = field(default_factory=dict)
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
        required_pools = ({SOURCE_POOLS[source] for source in self.enabled_sources
                           if source in SOURCE_POOLS}
                          if isinstance(self.enabled_sources, frozenset) else set())
        allocations = self.source_allocations
        if not isinstance(allocations, Mapping):
            raise ValueError("source quota allocations must be a mapping")
        allocation_pools = set(allocations.values())
        extra_pools = set(self.pools) - required_pools - allocation_pools
        if (type(self.enabled) is not bool or not isinstance(self.enabled_sources, frozenset)
                or not self.enabled_sources or not self.enabled_sources <= SOURCE_POOLS.keys()
                or not re.fullmatch(r"[a-f0-9]{64}", self.plan_fingerprint)
                or not required_pools <= set(self.pools)
                or extra_pools and any(
                    pool not in {
                        "google_rss": GOOGLE_SOURCE_ID,
                        YOUTUBE_OTHER_POOL: YOUTUBE_SOURCE_ID,
                    } or owner not in self.enabled_sources
                    for pool, owner in {
                        "google_rss": GOOGLE_SOURCE_ID,
                        YOUTUBE_OTHER_POOL: YOUTUBE_SOURCE_ID,
                    }.items() if pool in extra_pools
                )
                or extra_pools - {"google_rss", YOUTUBE_OTHER_POOL}
                or any(source not in self.enabled_sources
                       or SOURCE_ALLOCATION_POOLS.get(source) != pool
                       or pool not in self.pools or pool == SOURCE_POOLS[source]
                       for source, pool in allocations.items())
                or len(allocation_pools) != len(allocations)
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

    def __init__(self, store: AtomicStateStore, policy: AdmissionPolicy, *, state_key: str,
                 owner_manual_youtube_live: bool = False):
        if not callable(getattr(store, "transact", None)):
            raise TypeError("atomic state store required")
        if (not isinstance(policy, AdmissionPolicy) or not _OWNER.fullmatch(state_key)
                or type(owner_manual_youtube_live) is not bool
                or owner_manual_youtube_live and YOUTUBE_SOURCE_ID not in policy.enabled_sources):
            raise ValueError("validated server policy and state key required")
        self.store, self.policy, self.state_key = store, policy, state_key
        # Runtime-only owner authorization; intentionally excluded from policy state/digest.
        self.owner_manual_youtube_live = owner_manual_youtube_live
        self._pools = MappingProxyType(dict(policy.pools))
        self.policy_digest = self._policy_digest(policy)

    @staticmethod
    def _policy_digest(policy: AdmissionPolicy) -> str:
        config = asdict(policy)
        config["enabled_sources"] = sorted(policy.enabled_sources)
        config.pop("enabled")
        # Keep the established Google-only digest stable. Empty allocation
        # metadata did not exist in the persisted V1 policy document.
        if not config.get("source_allocations"):
            config.pop("source_allocations", None)
        return hashlib.sha256(json.dumps(
            config, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")).hexdigest()

    def migrate_bounded_quota_policy(self, previous_policy: AdmissionPolicy, *,
                                     authorization_note: str, now: datetime,
                                     allow_fetch_capacity_increase: bool = False) -> dict:
        """Apply a narrow quota or Fetch-capacity increase without resetting state.

        This owner-invoked migration is deliberately narrow: all workflow,
        timing, concurrency, and non-SerpApi policy fields must stay identical.
        Existing usage, jobs, reservations, and start history are not reset.
        """
        now = _utc(now)
        if (not isinstance(previous_policy, AdmissionPolicy)
                or not isinstance(authorization_note, str)
                or not 12 <= len(authorization_note) <= 256
                or "\x00" in authorization_note
                or type(allow_fetch_capacity_increase) is not bool):
            raise ValueError("previous policy and auditable owner authorization are required")
        previous_digest = self._policy_digest(previous_policy)
        previous_base, current_base = asdict(previous_policy), asdict(self.policy)
        previous_base["enabled_sources"] = sorted(previous_policy.enabled_sources)
        current_base["enabled_sources"] = sorted(self.policy.enabled_sources)
        previous_base.pop("enabled")
        current_base.pop("enabled")
        previous_pools, current_pools = previous_base.pop("pools"), current_base.pop("pools")
        previous_fetch_cap = previous_base.pop("max_starts_per_window")
        current_fetch_cap = current_base.pop("max_starts_per_window")
        previous_allocations = previous_base.pop("source_allocations", {})
        current_allocations = current_base.pop("source_allocations", {})
        previous_sources = set(previous_base.pop("enabled_sources"))
        current_sources = set(current_base.pop("enabled_sources"))
        added_bing_allocations = (
            not previous_allocations
            and current_allocations == {
                GOOGLE_SOURCE_ID: "serpapi_google", BING_SOURCE_ID: "serpapi_bing",
            }
            and current_sources - previous_sources == {BING_SOURCE_ID}
            and GOOGLE_SOURCE_ID in previous_sources
            and set(current_pools) - set(previous_pools)
            == {"serpapi_google", "serpapi_bing"}
        )
        added_youtube_source = (
            current_sources - previous_sources == {YOUTUBE_SOURCE_ID}
            and GOOGLE_SOURCE_ID in previous_sources
            and WIKIMEDIA_SOURCE_ID in previous_sources
            and previous_allocations == current_allocations
            and set(current_pools) - set(previous_pools)
            == {SOURCE_POOLS[YOUTUBE_SOURCE_ID], YOUTUBE_OTHER_POOL}
        )
        added_bing_and_youtube = (
            not previous_allocations
            and current_allocations == {
                GOOGLE_SOURCE_ID: "serpapi_google", BING_SOURCE_ID: "serpapi_bing",
            }
            and current_sources - previous_sources == {BING_SOURCE_ID, YOUTUBE_SOURCE_ID}
            and GOOGLE_SOURCE_ID in previous_sources
            and WIKIMEDIA_SOURCE_ID in previous_sources
            and set(current_pools) - set(previous_pools) == {
                "serpapi_google", "serpapi_bing",
                SOURCE_POOLS[YOUTUBE_SOURCE_ID], YOUTUBE_OTHER_POOL,
            }
        )
        increased_fetch_capacity = (
            allow_fetch_capacity_increase
            and type(previous_fetch_cap) is int and type(current_fetch_cap) is int
            and current_fetch_cap > previous_fetch_cap
        )
        if (previous_base != current_base
                or previous_fetch_cap != current_fetch_cap and not increased_fetch_capacity
                or current_sources != previous_sources
                and not (added_bing_allocations or added_youtube_source
                         or added_bing_and_youtube)
                or previous_allocations != current_allocations
                and not (added_bing_allocations or added_bing_and_youtube)
                or not (set(previous_pools) == set(current_pools)
                        or added_bing_allocations or added_youtube_source
                        or added_bing_and_youtube)):
            raise AdmissionDenied("POLICY_MIGRATION_SCOPE_INVALID")

        changed_pools = []
        budget_fields = {
            "daily_requests", "monthly_requests", "daily_cost_units", "monthly_cost_units",
        }
        for name, old_values in previous_pools.items():
            new_values = current_pools[name]
            if name != "serpapi":
                if old_values != new_values:
                    raise AdmissionDenied("POLICY_MIGRATION_SCOPE_INVALID")
                continue
            if set(old_values) != set(new_values):
                raise AdmissionDenied("POLICY_MIGRATION_SCOPE_INVALID")
            for limit_field, old_value in old_values.items():
                new_value = new_values[limit_field]
                if limit_field in budget_fields:
                    if new_value < old_value:
                        raise AdmissionDenied("POLICY_MIGRATION_NOT_MONOTONIC")
                elif new_value != old_value:
                    raise AdmissionDenied("POLICY_MIGRATION_SCOPE_INVALID")
            if any(old_values[limit_field] < new_values[limit_field]
                   for limit_field in budget_fields):
                changed_pools.append(name)
        if added_bing_allocations or added_bing_and_youtube:
            changed_pools.append("source_allocations")
        if added_youtube_source or added_bing_and_youtube:
            changed_pools.extend(sorted({SOURCE_POOLS[YOUTUBE_SOURCE_ID], YOUTUBE_OTHER_POOL}))
        if not changed_pools and not increased_fetch_capacity:
            raise AdmissionDenied("POLICY_MIGRATION_NO_CHANGE")

        def operation(state):
            if (not state or state.get("schema") != 1
                    or state.get("policy_digest") != previous_digest):
                raise AdmissionDenied("POLICY_MISMATCH")
            if state.get("operational_enabled") is not True:
                raise AdmissionDenied("FEATURE_DISABLED")
            jobs, inflight = state.get("jobs"), state.get("inflight")
            quota = state.get("quota")
            if not isinstance(jobs, dict) or not isinstance(inflight, dict) or not isinstance(quota, dict):
                raise AdmissionDenied("STATE_INVALID")
            for job in jobs.values():
                if (isinstance(job, dict) and job.get("state") in {"QUEUED", "RUNNING"}
                        and _parse(job.get("expires_at", "1970-01-01T00:00:00+00:00")) > now):
                    raise AdmissionDenied("POLICY_MIGRATION_BUSY")
            for slot in inflight.values():
                if (isinstance(slot, dict)
                        and _parse(slot.get("expires_at", "1970-01-01T00:00:00+00:00")) > now):
                    raise AdmissionDenied("POLICY_MIGRATION_BUSY")
            migrated_quota = deepcopy(quota)
            if added_bing_allocations or added_bing_and_youtube:
                # The old policy enabled only Google SerpApi. Preserve its
                # current day/month usage under Google's new allocation while
                # retaining the provider-level shared counters unchanged.
                for window, suffix in (
                    ("day", now.strftime("%Y-%m-%d")),
                    ("month", now.strftime("%Y-%m")),
                ):
                    legacy = migrated_quota.get(
                        f"serpapi:{window}:{suffix}", {"requests": 0, "cost": 0}
                    )
                    if (not isinstance(legacy, dict)
                            or type(legacy.get("requests")) is not int
                            or type(legacy.get("cost")) is not int
                            or legacy["requests"] < 0 or legacy["cost"] < 0):
                        raise AdmissionDenied("STATE_INVALID")
                    limit = self._pools["serpapi_google"]
                    request_limit = limit.daily_requests if window == "day" else limit.monthly_requests
                    cost_limit = limit.daily_cost_units if window == "day" else limit.monthly_cost_units
                    if legacy["requests"] > request_limit or legacy["cost"] > cost_limit:
                        raise AdmissionDenied("POLICY_MIGRATION_QUOTA_INCOMPATIBLE")
                    migrated_quota[f"serpapi_google:{window}:{suffix}"] = dict(legacy)
            for key, usage in migrated_quota.items():
                if not isinstance(key, str) or not isinstance(usage, dict):
                    raise AdmissionDenied("STATE_INVALID")
                parts = key.split(":")
                if (len(parts) != 3 or parts[0] not in self._pools
                        or parts[1] not in {"day", "month", "cycle"}):
                    raise AdmissionDenied("STATE_INVALID")
                if parts[1] == "cycle":
                    try:
                        cycle_date = date.fromisoformat(parts[2])
                    except ValueError:
                        raise AdmissionDenied("STATE_INVALID") from None
                    if cycle_date.isoformat() != parts[2]:
                        raise AdmissionDenied("STATE_INVALID")
                elif parts[1] == "month":
                    try:
                        month_date = date.fromisoformat(f"{parts[2]}-01")
                    except ValueError:
                        raise AdmissionDenied("STATE_INVALID") from None
                    if month_date.strftime("%Y-%m") != parts[2]:
                        raise AdmissionDenied("STATE_INVALID")
                requests, cost = usage.get("requests"), usage.get("cost")
                if (type(requests) is not int or type(cost) is not int
                        or requests < 0 or cost < 0):
                    raise AdmissionDenied("STATE_INVALID")
                limit = self._pools[parts[0]]
                request_limit = limit.daily_requests if parts[1] == "day" else limit.monthly_requests
                cost_limit = limit.daily_cost_units if parts[1] == "day" else limit.monthly_cost_units
                if requests > request_limit or cost > cost_limit:
                    raise AdmissionDenied("POLICY_MIGRATION_QUOTA_INCOMPATIBLE")
            audit = state.get("policy_migration_audit", [])
            if not isinstance(audit, list) or len(audit) >= 32:
                raise AdmissionDenied("POLICY_MIGRATION_AUDIT_UNAVAILABLE")
            record = {
                "schema": 1,
                "at": now.isoformat(),
                "from_policy_digest": previous_digest,
                "to_policy_digest": self.policy_digest,
                "changed_pools": changed_pools,
                "changed_admission_limits": (
                    (["max_starts_per_window"] if increased_fetch_capacity else [])
                    + (["enabled_sources"]
                       if added_youtube_source or added_bing_and_youtube else [])
                ),
                "authorization_note": authorization_note,
                "quota_preserved": True,
                "jobs_preserved": True,
                "reservations_preserved": True,
            }
            audit = [*audit, record]
            state["policy_digest"] = self.policy_digest
            state["quota"] = migrated_quota
            state["policy_migration_audit"] = audit
            return state, record

        return self.store.transact(self.state_key, operation)

    def migrate_legacy_google_live_policy(self, previous_policy: AdmissionPolicy, *,
                                          authorization_note: str,
                                          now: datetime) -> dict:
        """Upgrade the known pre-Bing Google ledger without discarding usage.

        This path is limited to the legacy Google/Wikimedia policy and an
        additive local policy that includes YouTube. It permits changed
        ceilings only when every persisted counter fits the destination pool.
        Existing provider-level SerpApi counters remain intact and are copied
        into Google's new source allocation before the policy digest changes.
        """
        now = _utc(now)
        if (not isinstance(previous_policy, AdmissionPolicy)
                or not isinstance(authorization_note, str)
                or not 12 <= len(authorization_note) <= 256
                or "\x00" in authorization_note):
            raise ValueError("previous policy and auditable owner authorization are required")

        previous_digest = self._policy_digest(previous_policy)
        previous_base, current_base = asdict(previous_policy), asdict(self.policy)
        previous_base["enabled_sources"] = sorted(previous_policy.enabled_sources)
        current_base["enabled_sources"] = sorted(self.policy.enabled_sources)
        previous_base.pop("enabled")
        current_base.pop("enabled")
        previous_pools = previous_base.pop("pools")
        current_pools = current_base.pop("pools")
        previous_fetch_cap = previous_base.pop("max_starts_per_window")
        current_fetch_cap = current_base.pop("max_starts_per_window")
        previous_allocations = previous_base.pop("source_allocations", {})
        current_allocations = current_base.pop("source_allocations", {})
        previous_sources = set(previous_base.pop("enabled_sources"))
        current_sources = set(current_base.pop("enabled_sources"))

        legacy_sources = {WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID}
        expected_allocations = (
            {GOOGLE_SOURCE_ID: "serpapi_google", BING_SOURCE_ID: "serpapi_bing"}
            if BING_SOURCE_ID in current_sources else {}
        )
        added_pools = set(current_pools) - set(previous_pools)
        expected_added_pools = (
            ({"serpapi_google", "serpapi_bing"} if expected_allocations else set())
            | ({SOURCE_POOLS[YOUTUBE_SOURCE_ID], YOUTUBE_OTHER_POOL}
               if YOUTUBE_SOURCE_ID in current_sources else set())
        )
        if (previous_sources != legacy_sources
                or previous_allocations
                or not legacy_sources <= current_sources
                or YOUTUBE_SOURCE_ID not in current_sources
                or current_sources - legacy_sources - {BING_SOURCE_ID, YOUTUBE_SOURCE_ID}
                or current_allocations != expected_allocations
                or set(previous_pools) != {"wikimedia", "serpapi", "google_rss"}
                or added_pools != expected_added_pools
                or current_fetch_cap < previous_fetch_cap
                or previous_base != current_base):
            raise AdmissionDenied("POLICY_MIGRATION_SCOPE_INVALID")

        budget_fields = {
            "daily_requests", "monthly_requests", "daily_cost_units", "monthly_cost_units",
        }
        for name, old_values in previous_pools.items():
            new_values = current_pools[name]
            if set(old_values) != set(new_values):
                raise AdmissionDenied("POLICY_MIGRATION_SCOPE_INVALID")
            for field_name, old_value in old_values.items():
                if field_name not in budget_fields and new_values[field_name] != old_value:
                    raise AdmissionDenied("POLICY_MIGRATION_SCOPE_INVALID")

        def operation(state):
            if (not state or state.get("schema") != 1
                    or state.get("policy_digest") != previous_digest):
                raise AdmissionDenied("POLICY_MISMATCH")
            if state.get("operational_enabled") is not True:
                raise AdmissionDenied("FEATURE_DISABLED")
            jobs, inflight, quota = (
                state.get("jobs"), state.get("inflight"), state.get("quota")
            )
            if not isinstance(jobs, dict) or not isinstance(inflight, dict) or not isinstance(quota, dict):
                raise AdmissionDenied("STATE_INVALID")
            for job in jobs.values():
                if (isinstance(job, dict) and job.get("state") in {"QUEUED", "RUNNING"}
                        and _parse(job.get("expires_at", "1970-01-01T00:00:00+00:00")) > now):
                    raise AdmissionDenied("POLICY_MIGRATION_BUSY")
            for slot in inflight.values():
                if (isinstance(slot, dict)
                        and _parse(slot.get("expires_at", "1970-01-01T00:00:00+00:00")) > now):
                    raise AdmissionDenied("POLICY_MIGRATION_BUSY")

            migrated_quota = deepcopy(quota)
            google_pool = current_allocations.get(GOOGLE_SOURCE_ID)
            for key, usage in tuple(migrated_quota.items()):
                if not isinstance(key, str) or not isinstance(usage, dict):
                    raise AdmissionDenied("STATE_INVALID")
                parts = key.split(":")
                if (len(parts) != 3 or parts[0] not in self._pools
                        or parts[1] not in {"day", "month", "cycle"}):
                    raise AdmissionDenied("STATE_INVALID")
                if parts[1] == "cycle":
                    try:
                        cycle_date = date.fromisoformat(parts[2])
                    except ValueError:
                        raise AdmissionDenied("STATE_INVALID") from None
                    if cycle_date.isoformat() != parts[2]:
                        raise AdmissionDenied("STATE_INVALID")
                elif parts[1] == "month":
                    try:
                        month_date = date.fromisoformat(f"{parts[2]}-01")
                    except ValueError:
                        raise AdmissionDenied("STATE_INVALID") from None
                    if month_date.strftime("%Y-%m") != parts[2]:
                        raise AdmissionDenied("STATE_INVALID")
                elif parts[1] == "day":
                    try:
                        day = date.fromisoformat(parts[2])
                    except ValueError:
                        raise AdmissionDenied("STATE_INVALID") from None
                    if day.isoformat() != parts[2]:
                        raise AdmissionDenied("STATE_INVALID")
                requests, cost = usage.get("requests"), usage.get("cost")
                if (type(requests) is not int or type(cost) is not int
                        or requests < 0 or cost < 0):
                    raise AdmissionDenied("STATE_INVALID")
                limit = self._pools[parts[0]]
                request_limit = limit.daily_requests if parts[1] == "day" else limit.monthly_requests
                cost_limit = limit.daily_cost_units if parts[1] == "day" else limit.monthly_cost_units
                if requests > request_limit or cost > cost_limit:
                    raise AdmissionDenied("POLICY_MIGRATION_QUOTA_INCOMPATIBLE")

                if parts[0] == "serpapi" and google_pool:
                    allocation_key = ":".join((google_pool, parts[1], parts[2]))
                    existing_allocation = migrated_quota.get(allocation_key)
                    if existing_allocation is not None and (
                            existing_allocation.get("requests") != requests
                            or existing_allocation.get("cost") != cost):
                        raise AdmissionDenied("POLICY_MIGRATION_QUOTA_INCOMPATIBLE")
                    allocation_limit = self._pools[google_pool]
                    allocation_requests = (
                        allocation_limit.daily_requests if parts[1] == "day"
                        else allocation_limit.monthly_requests
                    )
                    allocation_cost = (
                        allocation_limit.daily_cost_units if parts[1] == "day"
                        else allocation_limit.monthly_cost_units
                    )
                    if requests > allocation_requests or cost > allocation_cost:
                        raise AdmissionDenied("POLICY_MIGRATION_QUOTA_INCOMPATIBLE")
                    if existing_allocation is None:
                        migrated_quota[allocation_key] = deepcopy(usage)

            audit = state.get("policy_migration_audit", [])
            if not isinstance(audit, list) or len(audit) >= 32:
                raise AdmissionDenied("POLICY_MIGRATION_AUDIT_UNAVAILABLE")
            changed_pools = sorted(
                name for name, old_values in previous_pools.items()
                if old_values != asdict(self._pools[name])
            )
            changed_pools.extend(sorted(added_pools))
            if previous_allocations != current_allocations:
                changed_pools.append("source_allocations")
            changed_limits = []
            if previous_fetch_cap != current_fetch_cap:
                changed_limits.append("max_starts_per_window")
            if previous_sources != current_sources:
                changed_limits.append("enabled_sources")
            record = {
                "schema": 1,
                "at": now.isoformat(),
                "from_policy_digest": previous_digest,
                "to_policy_digest": self.policy_digest,
                "changed_pools": changed_pools,
                "changed_admission_limits": changed_limits,
                "migration_kind": "LEGACY_GOOGLE_TO_D6_YOUTUBE_V1",
                "authorization_note": authorization_note,
                "quota_preserved": True,
                "jobs_preserved": True,
                "reservations_preserved": True,
            }
            state["policy_digest"] = self.policy_digest
            state["quota"] = migrated_quota
            state["policy_migration_audit"] = [*audit, record]
            return state, record

        return self.store.transact(self.state_key, operation)

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

    def _pool_name(self, source_id: str, pool_name: str | None = None) -> str:
        if source_id not in self.policy.enabled_sources:
            raise AdmissionDenied("SOURCE_OR_ATTEMPT_INVALID")
        selected = SOURCE_POOLS[source_id] if pool_name is None else pool_name
        if (selected not in self._pools
                or selected != SOURCE_POOLS[source_id]
                and not (source_id == GOOGLE_SOURCE_ID and selected == "google_rss")
                and not (source_id == YOUTUBE_SOURCE_ID and selected == YOUTUBE_OTHER_POOL)):
            raise AdmissionDenied("SOURCE_POOL_NOT_ENABLED")
        return selected

    def max_attempt_seconds(self, source_id: str, pool_name: str | None = None) -> int:
        return self._pools[self._pool_name(source_id, pool_name)].attempt_timeout_seconds

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

    def last_attempt_reserved_at(self, source_id: str) -> str | None:
        """Read the latest durable pre-request reservation time for one source."""
        if source_id not in self.policy.enabled_sources:
            raise ValueError("configured source required")

        def read(state):
            if not state:
                return state, None
            if (state.get("schema") != 1
                    or state.get("policy_digest") != self.policy_digest):
                raise AdmissionDenied("STATE_INVALID")
            values = state.get("last_attempt_reserved_at", {})
            if not isinstance(values, dict):
                raise AdmissionDenied("STATE_INVALID")
            value = values.get(source_id)
            if value is None:
                return state, None
            if not isinstance(value, str):
                raise AdmissionDenied("STATE_INVALID")
            try:
                parsed = _parse(value)
            except (TypeError, ValueError):
                raise AdmissionDenied("STATE_INVALID") from None
            return state, parsed.isoformat()

        return self.store.transact(self.state_key, read)

    def last_provider_request_at(self, source_id: str) -> str | None:
        """Read the latest timestamp at which a guarded request was dispatched."""
        if source_id not in self.policy.enabled_sources:
            raise ValueError("configured source required")

        def read(state):
            if not state:
                return state, None
            if (state.get("schema") != 1
                    or state.get("policy_digest") != self.policy_digest):
                raise AdmissionDenied("STATE_INVALID")
            values = state.get("last_provider_request_at", {})
            if not isinstance(values, dict):
                raise AdmissionDenied("STATE_INVALID")
            value = values.get(source_id)
            if value is None:
                return state, None
            if not isinstance(value, str):
                raise AdmissionDenied("STATE_INVALID")
            try:
                parsed = _parse(value)
            except (TypeError, ValueError):
                raise AdmissionDenied("STATE_INVALID") from None
            return state, parsed.isoformat()

        return self.store.transact(self.state_key, read)

    @staticmethod
    def _serpapi_unattributed_usage(state, cycle_end: str | None) -> dict:
        """Read external provider usage reserved only against the shared pool."""
        zero = {"requests": 0, "cost": 0}
        value = state.get("serpapi_account_usage_unattributed")
        if value is None:
            return zero
        if not isinstance(value, dict):
            raise AdmissionDenied("STATE_INVALID")
        recorded_cycle = value.get("cycle_ends_at")
        requests, cost = value.get("requests"), value.get("cost")
        try:
            parsed_cycle = date.fromisoformat(recorded_cycle)
        except (TypeError, ValueError):
            raise AdmissionDenied("STATE_INVALID") from None
        if (parsed_cycle.isoformat() != recorded_cycle
                or type(requests) is not int or requests < 0
                or type(cost) is not int or cost < 0):
            raise AdmissionDenied("STATE_INVALID")
        if recorded_cycle != cycle_end:
            return zero
        return {"requests": requests, "cost": cost}

    def quota_runtime_snapshot(self, pool_names, *, source_ids=(), now: datetime) -> dict:
        """Read quota usage and provider-attempt times in one atomic state read."""
        now = _utc(now)
        if (not isinstance(pool_names, (tuple, list)) or not pool_names
                or any(not isinstance(name, str) or name not in self._pools
                       for name in pool_names)
                or len(pool_names) != len(set(pool_names))):
            raise ValueError("unique configured quota pools required")
        if (not isinstance(source_ids, (tuple, list))
                or any(not isinstance(source_id, str)
                       or source_id not in self.policy.enabled_sources
                       for source_id in source_ids)
                or len(source_ids) != len(set(source_ids))):
            raise ValueError("unique configured sources required")

        def snapshot(state, pool_name):
            limit = self._pools[pool_name]
            day_date = self._quota_day(pool_name, now)
            day, month = day_date.isoformat(), now.strftime("%Y-%m")
            uses_provider_cycle = (
                bool(self.policy.source_allocations)
                and (pool_name == "serpapi"
                     or pool_name in self.policy.source_allocations.values())
            )

            def usage(key):
                value = state.get("quota", {}).get(key, {"requests": 0, "cost": 0})
                if (not isinstance(value, dict)
                        or type(value.get("requests")) is not int
                        or type(value.get("cost")) is not int
                        or value["requests"] < 0 or value["cost"] < 0):
                    raise AdmissionDenied("STATE_INVALID")
                return value

            daily = usage(f"{pool_name}:day:{day}")
            cycle_end = state.get("serpapi_cycle_ends_at") if uses_provider_cycle else None
            if cycle_end is not None:
                try:
                    parsed_cycle_end = date.fromisoformat(cycle_end)
                except (TypeError, ValueError):
                    raise AdmissionDenied("STATE_INVALID") from None
                if parsed_cycle_end.isoformat() != cycle_end:
                    raise AdmissionDenied("STATE_INVALID")
                if parsed_cycle_end < now.date():
                    cycle_end = None
            monthly_key = (f"{pool_name}:cycle:{cycle_end}" if cycle_end
                           else f"{pool_name}:month:{month}")
            monthly = usage(monthly_key)
            unattributed = (self._serpapi_unattributed_usage(state, cycle_end)
                            if pool_name == "serpapi" else None)
            monthly_value = {
                "limit": limit.monthly_requests,
                "used": monthly["requests"],
                "remaining": max(0, limit.monthly_requests - monthly["requests"]),
                "units_limit": limit.monthly_cost_units,
                "units_reserved": monthly["cost"],
                "units_remaining": max(0, limit.monthly_cost_units - monthly["cost"]),
                "timezone": "UTC",
            }
            if pool_name == "serpapi":
                external = unattributed or {"requests": 0, "cost": 0}
                monthly_value["unattributed_external_requests"] = external["requests"]
                monthly_value["unattributed_external_cost_units"] = external["cost"]
            if uses_provider_cycle and not cycle_end:
                monthly_value.update(used=None, remaining=None)
            value = {
                "daily": {
                    "limit": limit.daily_requests,
                    "used": daily["requests"],
                    "remaining": max(0, limit.daily_requests - daily["requests"]),
                    "units_limit": limit.daily_cost_units,
                    "units_reserved": daily["cost"],
                    "units_remaining": max(0, limit.daily_cost_units - daily["cost"]),
                    "timezone": (str(YOUTUBE_PROVIDER_TIMEZONE)
                                 if pool_name in {SOURCE_POOLS[YOUTUBE_SOURCE_ID],
                                                  YOUTUBE_OTHER_POOL}
                                 else "UTC"),
                },
                "monthly": monthly_value,
            }
            if pool_name in {SOURCE_POOLS[YOUTUBE_SOURCE_ID], YOUTUBE_OTHER_POOL}:
                local_day = now.astimezone(YOUTUBE_PROVIDER_TIMEZONE).date()
                next_reset = datetime.combine(
                    local_day + timedelta(days=1), datetime.min.time(),
                    tzinfo=YOUTUBE_PROVIDER_TIMEZONE,
                )
                value["daily"]["reset_at"] = next_reset.astimezone(timezone.utc).isoformat()
                value["daily"]["timezone"] = str(YOUTUBE_PROVIDER_TIMEZONE)
                value["monthly"]["timezone"] = "UTC"
            if uses_provider_cycle:
                value["billing_cycle_ends_at"] = cycle_end
                value["billing_cycle_status"] = "KNOWN" if cycle_end else "UNKNOWN"
            allocated_sources = [
                source for source, allocation_pool in self.policy.source_allocations.items()
                if allocation_pool == pool_name
            ]
            if len(allocated_sources) == 1:
                value["source_id"] = allocated_sources[0]
            return value

        def timestamp(state, field_name, source_id):
            values = state.get(field_name, {})
            if not isinstance(values, dict):
                raise AdmissionDenied("STATE_INVALID")
            value = values.get(source_id)
            if value is None:
                return None
            if not isinstance(value, str):
                raise AdmissionDenied("STATE_INVALID")
            try:
                return _parse(value).isoformat()
            except (TypeError, ValueError):
                raise AdmissionDenied("STATE_INVALID") from None

        def read(state):
            if state and (state.get("schema") != 1
                          or state.get("policy_digest") != self.policy_digest
                          or not isinstance(state.get("quota"), dict)):
                raise AdmissionDenied("STATE_INVALID")
            return state, {
                "operational_enabled": (self.policy.enabled if not state else
                                        state.get("operational_enabled") is True),
                "pools": {name: snapshot(state, name) for name in pool_names},
                "last_attempt_reserved_at": {
                    source_id: timestamp(state, "last_attempt_reserved_at", source_id)
                    for source_id in source_ids
                },
                "last_provider_request_at": {
                    source_id: timestamp(state, "last_provider_request_at", source_id)
                    for source_id in source_ids
                },
            }

        return self.store.transact(self.state_key, read)

    def quota_snapshot(self, pool_name: str, *, now: datetime) -> dict:
        """Return bounded local reservation counts for one configured pool."""
        return self.quota_runtime_snapshot((pool_name,), now=now)["pools"][pool_name]

    def set_serpapi_billing_cycle(self, cycle_ends_at: str, *, now: datetime) -> None:
        """Pin combined Google/Bing reservations to the provider renewal boundary."""
        now = _utc(now)
        if (not self.policy.source_allocations
                or not isinstance(cycle_ends_at, str)):
            raise AdmissionDenied("QUOTA_CYCLE_UNKNOWN")
        try:
            cycle_date = date.fromisoformat(cycle_ends_at)
        except ValueError:
            raise AdmissionDenied("QUOTA_CYCLE_UNKNOWN") from None
        if cycle_date.isoformat() != cycle_ends_at or cycle_date < now.date():
            raise AdmissionDenied("QUOTA_CYCLE_UNKNOWN")

        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            if cycle_date < now.date():
                raise AdmissionDenied("QUOTA_CYCLE_UNKNOWN")
            previous_cycle = state.get("serpapi_cycle_ends_at")
            if previous_cycle == cycle_ends_at:
                return
            quota = state["quota"]
            if previous_cycle is None:
                # Conservatively carry the pre-migration UTC-month ledger into
                # the first provider cycle. Later renewal cycles start fresh.
                legacy_month = now.strftime("%Y-%m")
                cycle_keys = [f"serpapi:cycle:{cycle_ends_at}"] + [
                    f"{allocation_pool}:cycle:{cycle_ends_at}"
                    for allocation_pool in self.policy.source_allocations.values()
                ]
                if any(key in quota for key in cycle_keys):
                    raise AdmissionDenied("STATE_INVALID")
                if len(quota) + len(cycle_keys) > self.policy.max_records:
                    raise AdmissionDenied("QUOTA_LEDGER_CAPACITY")
                source_usage = quota.get(
                    f"serpapi:month:{legacy_month}", {"requests": 0, "cost": 0}
                )
                if (not isinstance(source_usage, dict)
                        or type(source_usage.get("requests")) is not int
                        or type(source_usage.get("cost")) is not int
                        or source_usage["requests"] < 0 or source_usage["cost"] < 0):
                    raise AdmissionDenied("STATE_INVALID")
                quota[f"serpapi:cycle:{cycle_ends_at}"] = dict(source_usage)
                for source_id, allocation_pool in self.policy.source_allocations.items():
                    existing = quota.get(f"{allocation_pool}:month:{legacy_month}",
                                         {"requests": 0, "cost": 0})
                    if (not isinstance(existing, dict)
                            or type(existing.get("requests")) is not int
                            or type(existing.get("cost")) is not int
                            or existing["requests"] < 0 or existing["cost"] < 0):
                        raise AdmissionDenied("STATE_INVALID")
                    quota[f"{allocation_pool}:cycle:{cycle_ends_at}"] = dict(existing)
            state["serpapi_cycle_ends_at"] = cycle_ends_at
        self._transaction(op)

    def reconcile_serpapi_account_usage(self, cycle_ends_at: str, monthly_used: int,
                                        *, now: datetime) -> None:
        """Reconcile provider counts without assigning unknown calls to a source.

        Before Bing is first enabled, all provider usage predates the Bing
        allocation and is conservatively assigned to Google. After that
        baseline, later provider usage beyond the local ledger is reserved in
        the shared monthly pool as unattributed external use. It never increases
        either source allocation. Account usage regression remains fail-closed.
        """
        now = _utc(now)
        if (not self.policy.source_allocations or not isinstance(cycle_ends_at, str)
                or type(monthly_used) is not int or not 0 <= monthly_used <= 1_000_000):
            raise AdmissionDenied("QUOTA_ACCOUNT_USAGE_INVALID")
        try:
            cycle_date = date.fromisoformat(cycle_ends_at)
        except ValueError:
            raise AdmissionDenied("QUOTA_CYCLE_UNKNOWN") from None
        if cycle_date.isoformat() != cycle_ends_at or cycle_date < now.date():
            raise AdmissionDenied("QUOTA_CYCLE_UNKNOWN")
        allocations = self.policy.source_allocations
        google_pool = allocations.get(GOOGLE_SOURCE_ID)
        if (google_pool != "serpapi_google"
                or allocations.get(BING_SOURCE_ID) != "serpapi_bing"
                or set(allocations) != {GOOGLE_SOURCE_ID, BING_SOURCE_ID}):
            raise AdmissionDenied("QUOTA_SOURCE_ALLOCATION_INVALID")

        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            if cycle_date < now.date():
                raise AdmissionDenied("QUOTA_CYCLE_UNKNOWN")
            if state.get("serpapi_cycle_ends_at") != cycle_ends_at:
                raise AdmissionDenied("QUOTA_CYCLE_UNKNOWN")
            if monthly_used > self._pools["serpapi"].monthly_requests:
                raise AdmissionDenied("QUOTA_ACCOUNT_USAGE_EXCEEDS_ALLOCATION")
            quota = state.get("quota")
            if not isinstance(quota, dict):
                raise AdmissionDenied("STATE_INVALID")
            shared_key = f"serpapi:cycle:{cycle_ends_at}"
            shared = quota.get(shared_key, {"requests": 0, "cost": 0})
            google_key = f"{google_pool}:cycle:{cycle_ends_at}"
            google = quota.get(google_key, {"requests": 0, "cost": 0})
            bing_key = f"{allocations[BING_SOURCE_ID]}:cycle:{cycle_ends_at}"
            bing = quota.get(bing_key, {"requests": 0, "cost": 0})
            external = self._serpapi_unattributed_usage(state, cycle_ends_at)
            if any(not isinstance(value, dict)
                   or type(value.get("requests")) is not int
                   or type(value.get("cost")) is not int
                   or value["requests"] < 0 or value["cost"] < 0
                   for value in (shared, google, bing)):
                raise AdmissionDenied("STATE_INVALID")
            if (google["requests"] + bing["requests"] + external["requests"]
                    != shared["requests"]
                    or google["cost"] + bing["cost"] + external["cost"]
                    != shared["cost"]):
                raise AdmissionDenied("QUOTA_ALLOCATION_STATE_MISMATCH")

            baseline_key = "serpapi_account_usage_baseline_cycle"
            last_used_key = "serpapi_account_usage_last_observed"
            baseline_cycle = state.get(baseline_key)
            last_observed = state.get(last_used_key)
            if baseline_cycle is not None:
                try:
                    parsed_baseline = date.fromisoformat(baseline_cycle)
                except (TypeError, ValueError):
                    raise AdmissionDenied("QUOTA_ACCOUNT_USAGE_STATE_INVALID") from None
                if (parsed_baseline.isoformat() != baseline_cycle
                        or parsed_baseline > cycle_date):
                    raise AdmissionDenied("QUOTA_ACCOUNT_USAGE_STATE_INVALID")
            elif last_observed is not None:
                raise AdmissionDenied("QUOTA_ACCOUNT_USAGE_STATE_INVALID")
            if baseline_cycle != cycle_ends_at:
                delta = max(0, monthly_used - shared["requests"])
                if baseline_cycle is None:
                    limit = self._pools[google_pool]
                    if (google["requests"] + delta > limit.monthly_requests
                            or google["cost"] + delta > limit.monthly_cost_units):
                        raise AdmissionDenied("QUOTA_ACCOUNT_USAGE_EXCEEDS_ALLOCATION")
                    google = dict(google)
                    google["requests"] += delta
                    google["cost"] += delta
                    quota[google_key] = google
                    external = {"requests": 0, "cost": 0}
                else:
                    external = {"requests": delta, "cost": delta}
                limit = self._pools["serpapi"]
                if (monthly_used > limit.monthly_requests
                        or shared["requests"] + delta > limit.monthly_requests
                        or shared["cost"] + delta > limit.monthly_cost_units):
                    raise AdmissionDenied("QUOTA_ACCOUNT_USAGE_EXCEEDS_ALLOCATION")
                shared = dict(shared)
                shared["requests"] += delta
                shared["cost"] += delta
                quota[shared_key] = shared
                state["serpapi_account_usage_unattributed"] = {
                    "cycle_ends_at": cycle_ends_at,
                    "requests": external["requests"],
                    "cost": external["cost"],
                }
                state[baseline_key] = cycle_ends_at
            else:
                if (type(last_observed) is not int or last_observed < 0
                        or monthly_used < last_observed):
                    raise AdmissionDenied("QUOTA_EXTERNAL_USAGE_UNRECONCILED")
                delta = max(0, monthly_used - shared["requests"])
                limit = self._pools["serpapi"]
                if (shared["requests"] + delta > limit.monthly_requests
                        or shared["cost"] + delta > limit.monthly_cost_units):
                    raise AdmissionDenied("QUOTA_ACCOUNT_USAGE_EXCEEDS_ALLOCATION")
                if delta:
                    shared = dict(shared)
                    shared["requests"] += delta
                    shared["cost"] += delta
                    quota[shared_key] = shared
                    external = {
                        "requests": external["requests"] + delta,
                        "cost": external["cost"] + delta,
                    }
                state["serpapi_account_usage_unattributed"] = {
                    "cycle_ends_at": cycle_ends_at,
                    "requests": external["requests"],
                    "cost": external["cost"],
                }

            state[last_used_key] = monthly_used
            state["serpapi_account_usage_checked_at"] = now.isoformat()

        self._transaction(op)

    def transact_owned(self, job_id: str, owner: str, fence: int, *, now: datetime, operation):
        """Run an internal pure mutation only while the current job lease is held."""
        now = _utc(now)
        if not callable(operation):
            raise ValueError("checkpoint operation required")
        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            return operation(job)
        return self._transaction(op)

    @staticmethod
    def _guard_clock(state, now):
        previous = state.get("last_observed_at")
        if previous is not None:
            previous = _parse(previous)
            if now < previous:
                regression = previous - now
                crossed_quota_day = (
                    now.date() != previous.date()
                    or now.astimezone(YOUTUBE_PROVIDER_TIMEZONE).date()
                    != previous.astimezone(YOUTUBE_PROVIDER_TIMEZONE).date()
                )
                if regression > _CLOCK_REGRESSION_TOLERANCE or crossed_quota_day:
                    raise AdmissionDenied("CLOCK_REGRESSION")
                # Firestore transactions can commit in a different order than
                # their callers captured wall time. Keep mutations monotonic by
                # evaluating bounded same-period skew at the durable high-water.
                now = previous
        state["last_observed_at"] = now.isoformat()
        return now

    @staticmethod
    def _effective_clock(state, now):
        """Read the monotonic high-water without making a read-only write."""
        previous = state.get("last_observed_at")
        return max(now, _parse(previous)) if previous is not None else now

    def _selected_job_sources(self, job):
        metadata = job.get("request_metadata", {})
        if not isinstance(metadata, dict):
            raise AdmissionDenied("JOB_STATE_INVALID")
        try:
            normalized = self._request_metadata(metadata if metadata else None)
        except ValueError:
            raise AdmissionDenied("JOB_STATE_INVALID") from None
        if normalized.get("kind") == "WIKIMEDIA_HISTORY":
            return [WIKIMEDIA_SOURCE_ID]
        return normalized.get("sources", sorted(self.policy.enabled_sources))

    def _completed_job_sources(self, job):
        selected = self._selected_job_sources(job)
        checkpoints = job.get("source_checkpoints", {})
        if not isinstance(checkpoints, dict):
            raise AdmissionDenied("JOB_STATE_INVALID")
        completed = {}
        for source_id in selected:
            entry = checkpoints.get(source_id)
            snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
            result = snapshot.get("result") if isinstance(snapshot, dict) else None
            if (isinstance(snapshot, dict) and snapshot.get("phase") == "COMPLETE"
                    and isinstance(result, dict)):
                completed[source_id] = result
        all_complete = len(completed) == len(selected)
        all_success = all_complete and all(
            value.get("status") == "SUCCESS" for value in completed.values()
        )
        return completed, all_complete, all_success

    def _terminal_state_for_job(self, job, fallback):
        completed, all_complete, all_success = self._completed_job_sources(job)
        if all_complete:
            return "COMPLETED" if all_success else "PARTIAL"
        if completed:
            return "PARTIAL"
        return fallback

    @staticmethod
    def _finish_recovered_job(job, state, now, *, terminal_reason=None):
        job.update(state=state, owner=None, lease_until=None,
                   recovery_available=False, updated_at=now.isoformat(),
                   continuation_available=False, finished_at=now.isoformat())
        if terminal_reason is None:
            job.pop("terminal_reason", None)
        else:
            job["terminal_reason"] = terminal_reason

    def _recover_terminal_job(self, job, fallback, reason, now):
        state = self._terminal_state_for_job(job, fallback)
        _, all_complete, _ = self._completed_job_sources(job)
        self._finish_recovered_job(
            job, state, now, terminal_reason=None if all_complete else reason
        )

    def _repair_job_lifecycle(self, job, now):
        """Make expired durable jobs observable without starting a worker."""
        if job.get("state") not in {"QUEUED", "RUNNING"}:
            return False
        try:
            expires_at = _parse(job["expires_at"])
            attempts = job["attempts"]
            continuation_available = job.get("continuation_available", False)
            if (type(attempts) is not int or attempts < 0
                    or type(continuation_available) is not bool):
                raise ValueError("invalid job attempt count")
        except (KeyError, TypeError, ValueError):
            raise AdmissionDenied("JOB_STATE_INVALID") from None

        if expires_at <= now:
            self._recover_terminal_job(
                job, "EXPIRED", "JOB_DEADLINE_EXPIRED", now
            )
            return True

        if job["state"] == "RUNNING":
            try:
                lease_until = _parse(job["lease_until"])
            except (KeyError, TypeError, ValueError):
                raise AdmissionDenied("JOB_STATE_INVALID") from None
            if lease_until > now:
                return False
            if attempts >= self.policy.max_attempts:
                self._recover_terminal_job(
                    job, "FAILED", "RECOVERY_ATTEMPTS_EXHAUSTED", now
                )
            else:
                job.update(state="QUEUED", owner=None, lease_until=None,
                           available_at=now.isoformat(), recovery_available=True,
                           updated_at=now.isoformat())
            return True

        try:
            available_at = _parse(job["available_at"])
        except (KeyError, TypeError, ValueError):
            raise AdmissionDenied("JOB_STATE_INVALID") from None
        if (available_at <= now and attempts >= self.policy.max_attempts
                and not continuation_available):
            self._recover_terminal_job(
                job, "FAILED", "RECOVERY_ATTEMPTS_EXHAUSTED", now
            )
            return True
        return False

    def _prune_expired_records(self, state, now):
        """Remove expired receipts and payloads without dropping live replay state."""
        jobs, keys = state.get("jobs"), state.get("keys")
        starts, inflight = state.get("starts"), state.get("inflight")
        if (not isinstance(jobs, dict) or not isinstance(keys, dict)
                or not isinstance(starts, list) or not isinstance(inflight, dict)):
            raise AdmissionDenied("STATE_INVALID")
        counts = {"idempotency_receipts": 0, "jobs": 0,
                  "source_checkpoints": 0, "invalid_checkpoints": 0,
                  "starts": 0, "inflight_slots": 0}

        for digest, receipt in list(keys.items()):
            if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or not isinstance(receipt, dict)
                    or not isinstance(receipt.get("job_id"), str)
                    or not _JOB_ID.fullmatch(receipt["job_id"])):
                raise AdmissionDenied("STATE_INVALID")
            try:
                expired = _parse(receipt["expires_at"]) <= now
            except (KeyError, TypeError, ValueError):
                raise AdmissionDenied("STATE_INVALID") from None
            if expired:
                del keys[digest]
                counts["idempotency_receipts"] += 1

        for job_id, job in list(jobs.items()):
            if (not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id)
                    or not isinstance(job, dict) or job.get("job_id") != job_id):
                raise AdmissionDenied("STATE_INVALID")
            try:
                job_expiry = _parse(job["expires_at"])
            except (KeyError, TypeError, ValueError):
                raise AdmissionDenied("STATE_INVALID") from None
            checkpoints = job.get("source_checkpoints", {})
            if not isinstance(checkpoints, dict):
                raise AdmissionDenied("STATE_INVALID")
            for source_id, checkpoint in list(checkpoints.items()):
                try:
                    if (not isinstance(source_id, str) or not source_id
                            or not isinstance(checkpoint, dict)):
                        raise ValueError("invalid checkpoint metadata")
                    expiry = _parse(checkpoint["retain_until"])
                except (KeyError, TypeError, ValueError):
                    del checkpoints[source_id]
                    counts["invalid_checkpoints"] += 1
                    continue
                if expiry <= now:
                    del checkpoints[source_id]
                    counts["source_checkpoints"] += 1
                elif expiry.isoformat() != checkpoint["retain_until"]:
                    checkpoint["retain_until"] = expiry.isoformat()

            self._repair_job_lifecycle(job, now)

        referenced = {receipt["job_id"] for receipt in keys.values()}
        for job_id, job in list(jobs.items()):
            job_expiry = _parse(job["expires_at"])
            checkpoints = job.get("source_checkpoints", {})
            if job_expiry <= now and job_id not in referenced and not checkpoints:
                del jobs[job_id]
                counts["jobs"] += 1

        start_cutoff = now - timedelta(seconds=self.policy.window_seconds)
        kept_starts = []
        for value in starts:
            try:
                if _parse(value) > start_cutoff:
                    kept_starts.append(value)
                else:
                    counts["starts"] += 1
            except (TypeError, ValueError):
                raise AdmissionDenied("STATE_INVALID") from None
        state["starts"] = kept_starts

        for slot_id, slot in list(inflight.items()):
            if not isinstance(slot_id, str) or not isinstance(slot, dict):
                raise AdmissionDenied("STATE_INVALID")
            try:
                expired = _parse(slot["expires_at"]) <= now
            except (KeyError, TypeError, ValueError):
                raise AdmissionDenied("STATE_INVALID") from None
            if expired:
                del inflight[slot_id]
                counts["inflight_slots"] += 1
        return counts

    def prune_expired(self, *, now: datetime) -> dict[str, int]:
        """Run bounded cleanup while Fetch is disabled or enabled.

        Source payloads expire independently from idempotency receipts. This is
        an internal maintenance operation and must only be called by a trusted
        server-side runner.
        """
        now = _utc(now)
        empty = {"idempotency_receipts": 0, "jobs": 0,
                 "source_checkpoints": 0, "invalid_checkpoints": 0,
                 "starts": 0, "inflight_slots": 0}

        def operation(state):
            if not state:
                return state, empty
            if (state.get("schema") != 1
                    or state.get("policy_digest") != self.policy_digest):
                raise AdmissionDenied("POLICY_MISMATCH")
            nonlocal now
            now = self._guard_clock(state, now)
            return state, self._prune_expired_records(state, now)

        return self.store.transact(self.state_key, operation)

    def _request_metadata(self, value):
        if value is None:
            return {}
        if (isinstance(value, dict) and set(value) == {"kind", "history_days"}
                and value.get("kind") == "WIKIMEDIA_HISTORY"
                and type(value.get("history_days")) is int
                and 1 <= value["history_days"] <= 2):
            return {"kind": "WIKIMEDIA_HISTORY", "history_days": value["history_days"]}
        allowed = {"sources", "owner_fresh_acceptance", "manual_source_refresh"}
        if (not isinstance(value, dict)
                or "sources" not in value
                or not set(value) <= allowed
                or not isinstance(value.get("sources"), list)
                or not 1 <= len(value["sources"]) <= len(SOURCE_POOLS)
                or any(not isinstance(source, str) for source in value["sources"])
                or len(value["sources"]) != len(set(value["sources"]))
                or not set(value["sources"]) <= self.policy.enabled_sources):
            raise ValueError("invalid bounded Fetch request metadata")
        metadata = {"sources": sorted(value["sources"])}
        if "owner_fresh_acceptance" in value:
            if (value.get("owner_fresh_acceptance") != OWNER_FRESH_ACCEPTANCE_MARKER
                    or set(metadata["sources"]) != set(SOURCE_POOLS)):
                raise ValueError("invalid owner fresh acceptance metadata")
            metadata["owner_fresh_acceptance"] = OWNER_FRESH_ACCEPTANCE_MARKER
        if "manual_source_refresh" in value:
            if value.get("manual_source_refresh") != MANUAL_SOURCE_REFRESH_MARKER:
                raise ValueError("invalid manual source refresh metadata")
            metadata["manual_source_refresh"] = MANUAL_SOURCE_REFRESH_MARKER
        return metadata

    def lookup_idempotency(self, idempotency_key: str, *, now: datetime,
                           request_metadata=None) -> dict | None:
        """Read an existing idempotency receipt without admitting new work."""
        now = _utc(now)
        if not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
            raise ValueError("invalid idempotency key")
        request_metadata = self._request_metadata(request_metadata)
        if not self.policy.enabled:
            raise AdmissionDenied("FEATURE_DISABLED")
        digest = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()

        def operation(state):
            if not state:
                return state, None
            if state.get("schema") != 1:
                raise AdmissionDenied("STATE_VERSION_UNSUPPORTED")
            if state.get("policy_digest") != self.policy_digest:
                raise AdmissionDenied("POLICY_MISMATCH")
            if state.get("operational_enabled") is not True:
                raise AdmissionDenied("FEATURE_DISABLED")
            jobs, keys = state.get("jobs"), state.get("keys")
            if not isinstance(jobs, dict) or not isinstance(keys, dict):
                raise AdmissionDenied("STATE_INVALID")
            receipt = keys.get(digest)
            if receipt is None:
                return state, None
            if not isinstance(receipt, dict) or not isinstance(receipt.get("expires_at"), str):
                raise AdmissionDenied("STATE_INVALID")
            try:
                if _parse(receipt["expires_at"]) <= now:
                    return state, None
            except (TypeError, ValueError):
                raise AdmissionDenied("STATE_INVALID") from None
            job_id = receipt.get("job_id")
            job = jobs.get(job_id) if isinstance(job_id, str) else None
            if not isinstance(job, dict) or job.get("job_id") != job_id:
                raise AdmissionDenied("STATE_INVALID")
            stored = job.get("request_metadata", {})
            if not isinstance(stored, dict):
                raise AdmissionDenied("STATE_INVALID")
            comparable = {key: value for key, value in stored.items()
                          if key != "owner_fresh_acceptance"}
            if comparable != request_metadata:
                raise AdmissionDenied("IDEMPOTENCY_KEY_CONFLICT")
            return state, {"job_id": job_id, "status": job.get("state", "EXPIRED")}

        return self.store.transact(self.state_key, operation)

    def admit(self, idempotency_key: str, *, now: datetime, request_metadata=None,
              owner_fresh_acceptance_once: bool = False) -> dict:
        now = _utc(now)
        if not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
            raise ValueError("invalid idempotency key")
        if type(owner_fresh_acceptance_once) is not bool:
            raise ValueError("owner fresh acceptance policy must be boolean")
        request_metadata = self._request_metadata(request_metadata)
        if not self.policy.enabled:
            raise AdmissionDenied("FEATURE_DISABLED")
        digest = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
        new_id = secrets.token_hex(16)

        def same_request(job):
            stored = job.get("request_metadata", {})
            if not isinstance(stored, dict):
                return False
            return {key: value for key, value in stored.items()
                    if key != "owner_fresh_acceptance"} == request_metadata

        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            jobs, keys = state["jobs"], state["keys"]
            self._prune_expired_records(state, now)
            existing = keys.get(digest)
            if existing:
                job = jobs.get(existing["job_id"], {})
                if not same_request(job):
                    raise AdmissionDenied("IDEMPOTENCY_KEY_CONFLICT")
                return {"job_id": existing["job_id"], "result": "REPLAY",
                        "status": job.get("state", "EXPIRED"),
                        "created_at": job.get("created_at"),
                        "owner_fresh_acceptance": (
                            job.get("request_metadata", {}).get("owner_fresh_acceptance")
                            == OWNER_FRESH_ACCEPTANCE_MARKER
                        )}
            active = next((job for job in jobs.values()
                           if job["state"] in {"QUEUED", "RUNNING"}
                           and _parse(job["expires_at"]) > now
                           and job["plan_fingerprint"] == self.policy.plan_fingerprint
                           and same_request(job)), None)
            if len(keys) >= self.policy.max_records:
                raise AdmissionDenied("RECEIPT_CAPACITY")
            if active:
                job_id = active["job_id"]
                result = "JOINED"
                owner_fresh_acceptance = (
                    active.get("request_metadata", {}).get("owner_fresh_acceptance")
                    == OWNER_FRESH_ACCEPTANCE_MARKER
                )
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
                stored_metadata = dict(request_metadata)
                owner_fresh_acceptance = False
                acceptance_sources = set(request_metadata.get("sources", []))
                if owner_fresh_acceptance_once and acceptance_sources == set(SOURCE_POOLS):
                    acceptance_state = state.get("d6_owner_fresh_acceptance")
                    if acceptance_state is None:
                        owner_fresh_acceptance = True
                        stored_metadata["owner_fresh_acceptance"] = OWNER_FRESH_ACCEPTANCE_MARKER
                        state["d6_owner_fresh_acceptance"] = {
                            "schema": 1,
                            "job_id": job_id,
                            "assigned_at": now.isoformat(),
                        }
                    elif (not isinstance(acceptance_state, dict)
                            or acceptance_state.get("schema") != 1
                            or not isinstance(acceptance_state.get("job_id"), str)
                            or not _JOB_ID.fullmatch(acceptance_state["job_id"])
                            or not isinstance(acceptance_state.get("assigned_at"), str)):
                        raise AdmissionDenied("OWNER_ACCEPTANCE_STATE_INVALID")
                jobs[job_id] = {"job_id": job_id, "state": "QUEUED", "owner": None,
                                "fence": 0, "attempts": 0, "lease_until": None,
                                "created_at": now.isoformat(),
                                "available_at": now.isoformat(),
                                "expires_at": (now + timedelta(seconds=self.policy.max_job_seconds)).isoformat(),
                                "plan_fingerprint": self.policy.plan_fingerprint,
                                "request_metadata": stored_metadata, "status_polls": 0,
                                "recovery_available": False, "continuation_available": False,
                                "receipts": {}}
                starts.append(now.isoformat())
            keys[digest] = {"job_id": job_id,
                            "expires_at": (now + timedelta(seconds=self.policy.receipt_seconds)).isoformat()}
            return {"job_id": job_id, "result": result,
                    "status": jobs[job_id]["state"],
                    "created_at": jobs.get(job_id, {}).get("created_at"),
                    "owner_fresh_acceptance": owner_fresh_acceptance}
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
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            if not isinstance(job, dict):
                return state, None
            polls = job.get("status_polls", 0)
            if type(polls) is not int or polls < 0:
                raise AdmissionDenied("JOB_STATE_INVALID")
            if polls >= self.policy.max_status_polls:
                raise AdmissionDenied("STATUS_POLL_LIMIT")
            job["status_polls"] = polls + 1
            self._repair_job_lifecycle(job, now)
            return state, deepcopy(job)
        return self.store.transact(self.state_key, operation)

    def list_jobs(self, *, limit: int, now: datetime) -> list[dict]:
        """Return a bounded newest-first job snapshot without charging status polls."""
        now = _utc(now)
        if type(limit) is not int or not 1 <= limit <= min(100, self.policy.max_records):
            raise ValueError("bounded job-history limit required")

        def operation(state):
            if not state:
                return state, []
            if (state.get("schema") != 1
                    or state.get("policy_digest") != self.policy_digest
                    or not isinstance(state.get("jobs"), dict)):
                raise AdmissionDenied("STATE_INVALID")
            observed_now = now
            effective_now = self._effective_clock(state, observed_now)
            jobs = []
            lifecycle_changed = False
            for job_id, job in state["jobs"].items():
                if (not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id)
                        or not isinstance(job, dict) or job.get("job_id") != job_id):
                    raise AdmissionDenied("STATE_INVALID")
                lifecycle_changed |= self._repair_job_lifecycle(job, effective_now)
                try:
                    created = _parse(job["created_at"])
                    updated = _parse(job.get("updated_at") or job["created_at"])
                except (KeyError, TypeError, ValueError):
                    raise AdmissionDenied("STATE_INVALID") from None
                jobs.append((updated, created, deepcopy(job)))
            if lifecycle_changed:
                self._guard_clock(state, observed_now)
            jobs.sort(key=lambda item: (item[0], item[1], item[2]["job_id"]), reverse=True)
            return state, [item[2] for item in jobs[:limit]]

        return self.store.transact(self.state_key, operation)

    def claim(self, job_id: str, owner: str, *, now: datetime) -> dict:
        now = _utc(now)
        if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
            raise ValueError("invalid worker identity")

        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            return self._claim_job(job, owner, now)
        return self._transaction(op)

    def claim_job(self, job_id: str, owner: str, *, now: datetime) -> dict:
        """Claim one named job and persist terminal lifecycle repair atomically."""
        now = _utc(now)
        if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
            raise ValueError("invalid worker identity")

        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            if not job:
                return {"denied": "JOB_EXPIRED"}
            if job.get("state") not in {"QUEUED", "RUNNING"}:
                return {"denied": "JOB_TERMINAL"}
            expires_at = _parse(job["expires_at"])
            if expires_at <= now:
                self._recover_terminal_job(
                    job, "EXPIRED", "JOB_DEADLINE_EXPIRED", now,
                )
                return {"denied": "JOB_EXPIRED"}
            if now < _parse(job["created_at"]):
                return {"denied": "JOB_EXPIRED"}
            if job["state"] == "RUNNING":
                if _parse(job["lease_until"]) > now:
                    raise AdmissionDenied("LEASE_BUSY")
            elif job["state"] == "QUEUED":
                if _parse(job["available_at"]) > now:
                    raise AdmissionDenied("JOB_DEFERRED")
            else:
                return {"denied": "JOB_TERMINAL"}

            attempts = job.get("attempts")
            continuation_available = job.get("continuation_available", False)
            if (type(attempts) is not int or attempts < 0
                    or type(continuation_available) is not bool):
                raise AdmissionDenied("JOB_STATE_INVALID")
            if attempts >= self.policy.max_attempts and not continuation_available:
                self._recover_terminal_job(
                    job, "FAILED", "RECOVERY_ATTEMPTS_EXHAUSTED", now,
                )
                return {"denied": "JOB_TERMINAL"}
            return {"claim": self._claim_job(job, owner, now)}

        result = self._transaction(op)
        if "denied" in result:
            raise AdmissionDenied(result["denied"])
        return result["claim"]

    def _claim_job(self, job, owner, now):
        if not job or _parse(job["expires_at"]) <= now or now < _parse(job["created_at"]):
            raise AdmissionDenied("JOB_EXPIRED")
        if job["state"] == "RUNNING" and _parse(job["lease_until"]) > now:
            raise AdmissionDenied("LEASE_BUSY")
        if job["state"] not in {"QUEUED", "RUNNING"}:
            raise AdmissionDenied("JOB_TERMINAL")
        if job["state"] == "QUEUED" and _parse(job["available_at"]) > now:
            raise AdmissionDenied("JOB_DEFERRED")
        continuation_available = job.get("continuation_available", False)
        if type(continuation_available) is not bool:
            raise AdmissionDenied("JOB_STATE_INVALID")
        if job["attempts"] >= self.policy.max_attempts and not continuation_available:
            raise AdmissionDenied("ATTEMPTS_EXHAUSTED")
        job.update(state="RUNNING", owner=owner, fence=job["fence"] + 1,
                   attempts=job["attempts"] + (0 if continuation_available else 1),
                   lease_until=min(now + timedelta(seconds=self.policy.lease_seconds),
                                   _parse(job["expires_at"])).isoformat(),
                   recovery_available=False, continuation_available=False,
                   updated_at=now.isoformat())
        return {"job_id": job["job_id"], "fence": job["fence"],
                "lease_until": job["lease_until"], "expires_at": job["expires_at"],
                "request_metadata": deepcopy(job.get("request_metadata", {}))}

    def claim_next(self, owner: str, *, now: datetime) -> dict | None:
        """Claim one due job or reclaim an expired lease, in the same transaction."""
        now = _utc(now)
        if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
            raise ValueError("invalid worker identity")
        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            for job in sorted(state["jobs"].values(), key=lambda x: (x["created_at"], x["job_id"])):
                if job["state"] not in {"QUEUED", "RUNNING"}:
                    continue
                if _parse(job["expires_at"]) <= now:
                    self._recover_terminal_job(
                        job, "EXPIRED", "JOB_DEADLINE_EXPIRED", now
                    )
                    continue
                due = (job["state"] == "QUEUED" and _parse(job["available_at"]) <= now
                       or job["state"] == "RUNNING" and _parse(job["lease_until"]) <= now)
                continuation_available = job.get("continuation_available", False)
                if type(continuation_available) is not bool:
                    raise AdmissionDenied("JOB_STATE_INVALID")
                if (due and job["attempts"] >= self.policy.max_attempts
                        and not continuation_available):
                    self._recover_terminal_job(
                        job, "FAILED", "RECOVERY_ATTEMPTS_EXHAUSTED", now
                    )
                elif due:
                    return self._claim_job(job, owner, now)
            return None
        return self._transaction(op)

    def defer(self, job_id: str, owner: str, fence: int, *, now: datetime,
              not_before: datetime) -> None:
        """Release a lease with a bounded retry time; never truncate Retry-After."""
        now, not_before = _utc(now), _utc(not_before)
        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            if (not job or job["state"] != "RUNNING" or job["owner"] != owner
                    or type(fence) is not int or job["fence"] != fence
                    or _parse(job["lease_until"]) <= now or _parse(job["expires_at"]) <= now):
                raise AdmissionDenied("LEASE_LOST")
            if not now < not_before < _parse(job["expires_at"]):
                raise AdmissionDenied("RETRY_OUTSIDE_DEADLINE")
            job.update(state="QUEUED", owner=None, lease_until=None,
                       available_at=not_before.isoformat(), recovery_available=False,
                       continuation_available=False,
                       updated_at=now.isoformat())
        self._transaction(op)

    def yield_for_continuation(self, job_id: str, owner: str, fence: int, *,
                               now: datetime) -> None:
        """Release a fenced source slice without consuming a retry attempt."""
        now = _utc(now)

        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            job.update(state="QUEUED", owner=None, lease_until=None,
                       available_at=now.isoformat(), recovery_available=True,
                       continuation_available=True, updated_at=now.isoformat())

        self._transaction(op)

    def heartbeat(self, job_id: str, owner: str, fence: int, *, now: datetime) -> str:
        """Extend only the currently fenced lease, never past the job deadline."""
        now = _utc(now)
        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
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
                        attempt_id: str, now: datetime, pool_name: str | None = None) -> str:
        """NEW permits exactly one outbound attempt. REPLAY never permits a resend."""
        now = _utc(now)
        if (source_id not in self.policy.enabled_sources or not isinstance(attempt_id, str)
                or not _KEY.fullmatch(attempt_id)):
            raise AdmissionDenied("SOURCE_OR_ATTEMPT_INVALID")
        pool = self._pool_name(source_id, pool_name)
        limit = self._pools[pool]
        allocation_pool = self.policy.source_allocations.get(source_id)
        allocation = self._pools.get(allocation_pool) if allocation_pool else None
        receipt_id = hashlib.sha256(attempt_id.encode("ascii")).hexdigest()

        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            existing = job["receipts"].get(receipt_id)
            if existing:
                if existing["source_id"] != source_id or existing.get("pool") != pool:
                    raise AdmissionDenied("ATTEMPT_ID_CONFLICT")
                return "REPLAY"
            if len(job["receipts"]) >= self.policy.max_records:
                raise AdmissionDenied("ATTEMPT_CAPACITY")
            day_date = self._quota_day(pool, now)
            day, month = day_date.isoformat(), now.strftime("%Y-%m")
            quota = state["quota"]
            cycle_end = state.get("serpapi_cycle_ends_at")
            if self.policy.source_allocations and pool == "serpapi":
                try:
                    cycle_date = date.fromisoformat(cycle_end) if cycle_end else None
                except (TypeError, ValueError):
                    cycle_date = None
                if (cycle_date is None or cycle_date.isoformat() != cycle_end
                        or cycle_date < now.date()):
                    raise AdmissionDenied("QUOTA_CYCLE_UNKNOWN")
            # Never delete a newer window: workers can straddle a UTC boundary.
            daily_key = f"{pool}:day:{day}"
            monthly_key = (f"{pool}:cycle:{cycle_end}" if pool == "serpapi" and cycle_end
                           else f"{pool}:month:{month}")
            # Once the server clock has advanced, older windows can no longer
            # affect admission; prune
            # only canonical, known-pool keys strictly before the active window.
            # Unknown/future/malformed entries are retained so corrupt state can
            # consume capacity and fail closed rather than being silently erased.
            for key in list(quota):
                parts = key.split(":") if isinstance(key, str) else ()
                if len(parts) != 3 or parts[0] not in self._pools:
                    continue
                quota_pool, window_kind, window_value = parts
                try:
                    if window_kind == "day":
                        parsed = date.fromisoformat(window_value)
                        pool_day = self._quota_day(quota_pool, now).isoformat()
                        if parsed.isoformat() == window_value and window_value < pool_day:
                            del quota[key]
                    elif window_kind == "month":
                        parsed = date.fromisoformat(f"{window_value}-01")
                        is_youtube_history = quota_pool in {
                            SOURCE_POOLS[YOUTUBE_SOURCE_ID], YOUTUBE_OTHER_POOL,
                        }
                        if (parsed.strftime("%Y-%m") == window_value and window_value < month
                                and not is_youtube_history):
                            del quota[key]
                    elif window_kind == "cycle" and cycle_end:
                        parsed = date.fromisoformat(window_value)
                        if parsed.isoformat() == window_value and window_value < cycle_end:
                            del quota[key]
                except ValueError:
                    continue
            allocation_monthly_key = (f"{allocation_pool}:cycle:{cycle_end}"
                                      if allocation is not None and cycle_end
                                      else f"{allocation_pool}:month:{month}")
            allocation_keys = (() if allocation is None else (
                f"{allocation_pool}:day:{day}", allocation_monthly_key,
            ))
            required_keys = (daily_key, monthly_key, *allocation_keys)
            if len(quota) + sum(key not in quota for key in required_keys) > self.policy.max_records:
                raise AdmissionDenied("QUOTA_LEDGER_CAPACITY")
            daily = quota.setdefault(daily_key, {"requests": 0, "cost": 0})
            monthly = quota.setdefault(monthly_key, {"requests": 0, "cost": 0})
            allocation_daily = (quota.setdefault(allocation_keys[0], {"requests": 0, "cost": 0})
                                if allocation is not None else None)
            allocation_monthly = (quota.setdefault(allocation_keys[1], {"requests": 0, "cost": 0})
                                  if allocation is not None else None)
            manual_youtube = self.owner_manual_youtube_live and source_id == YOUTUBE_SOURCE_ID
            # The one-time all-source acceptance may overrun daily application
            # caps, but the attempt still increments every ledger bucket and
            # monthly provider/source allocations remain hard limits.
            owner_fresh_acceptance = (
                isinstance(job.get("request_metadata"), dict)
                and job["request_metadata"].get("owner_fresh_acceptance")
                == OWNER_FRESH_ACCEPTANCE_MARKER
            )
            daily_exhausted = (
                daily["requests"] >= limit.daily_requests
                or daily["cost"] + limit.cost_per_attempt > limit.daily_cost_units
            )
            monthly_exhausted = (
                monthly["requests"] >= limit.monthly_requests
                or monthly["cost"] + limit.cost_per_attempt > limit.monthly_cost_units
            )
            if (not manual_youtube and (
                    monthly_exhausted or (daily_exhausted and not owner_fresh_acceptance))):
                raise AdmissionDenied("QUOTA_EXHAUSTED")
            if allocation is not None:
                allocation_daily_exhausted = (
                    allocation_daily["requests"] >= allocation.daily_requests
                    or allocation_daily["cost"] + allocation.cost_per_attempt
                    > allocation.daily_cost_units
                )
                allocation_monthly_exhausted = (
                    allocation_monthly["requests"] >= allocation.monthly_requests
                    or allocation_monthly["cost"] + allocation.cost_per_attempt
                    > allocation.monthly_cost_units
                )
                if (allocation_monthly_exhausted
                        or (allocation_daily_exhausted and not owner_fresh_acceptance)):
                    raise AdmissionDenied("SOURCE_ALLOCATION_EXHAUSTED")
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
            for bucket in (allocation_daily, allocation_monthly):
                if bucket is not None:
                    bucket["requests"] += 1
                    bucket["cost"] += allocation.cost_per_attempt
            job["receipts"][receipt_id] = {"source_id": source_id, "pool": pool,
                                          "allocation_pool": allocation_pool,
                                          "reserved_at": now.isoformat()}
            last_attempts = state.setdefault("last_attempt_reserved_at", {})
            if not isinstance(last_attempts, dict):
                raise AdmissionDenied("STATE_INVALID")
            last_attempts[source_id] = now.isoformat()
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
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            if receipt_id not in job["receipts"]:
                raise AdmissionDenied("ATTEMPT_NOT_FOUND")
            return state["inflight"].pop(f"{job_id}:{receipt_id}", None) is not None
        return self._transaction(op)

    @staticmethod
    def _quota_day(pool_name: str, now: datetime) -> date:
        if pool_name in {SOURCE_POOLS[YOUTUBE_SOURCE_ID], YOUTUBE_OTHER_POOL}:
            return now.astimezone(YOUTUBE_PROVIDER_TIMEZONE).date()
        return now.date()

    def check_attempt(self, job_id: str, owner: str, fence: int, *, attempt_id: str,
                      source_id: str, now: datetime, pool_name: str | None = None) -> datetime:
        """Recheck the switch, lease and reservation immediately before outbound I/O."""
        now = _utc(now)
        if not isinstance(attempt_id, str) or not _KEY.fullmatch(attempt_id):
            raise ValueError("invalid attempt identity")
        receipt_id = hashlib.sha256(attempt_id.encode("ascii")).hexdigest()
        pool = self._pool_name(source_id, pool_name)
        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            receipt = job["receipts"].get(receipt_id)
            slot = state["inflight"].get(f"{job_id}:{receipt_id}")
            if (not receipt or receipt["source_id"] != source_id
                    or receipt.get("pool") != pool or not slot or slot.get("pool") != pool
                    or _parse(slot["expires_at"]) <= now):
                raise AdmissionDenied("ATTEMPT_NOT_ACTIVE")
            return min(_parse(slot["expires_at"]), _parse(job["lease_until"]),
                       _parse(job["expires_at"]))
        return self._transaction(op)

    def record_provider_request_started(self, job_id: str, owner: str, fence: int, *,
                                        attempt_id: str, source_id: str, now: datetime,
                                        pool_name: str | None = None) -> None:
        """Persist request dispatch time after the reservation and before network I/O."""
        now = _utc(now)
        if not isinstance(attempt_id, str) or not _KEY.fullmatch(attempt_id):
            raise ValueError("invalid attempt identity")
        pool = self._pool_name(source_id, pool_name)
        receipt_id = hashlib.sha256(attempt_id.encode("ascii")).hexdigest()

        def operation(state):
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            receipt = job["receipts"].get(receipt_id)
            slot_key = f"{job_id}:{receipt_id}"
            slot = state["inflight"].get(slot_key)
            if (not receipt or receipt.get("source_id") != source_id
                    or receipt.get("pool") != pool or not slot or slot.get("pool") != pool
                    or _parse(slot["expires_at"]) <= now):
                raise AdmissionDenied("ATTEMPT_NOT_ACTIVE")
            values = state.setdefault("last_provider_request_at", {})
            if not isinstance(values, dict):
                raise AdmissionDenied("STATE_INVALID")
            values[source_id] = now.isoformat()

        self._transaction(operation)

    def finish(self, job_id: str, owner: str, fence: int, *, now: datetime,
               partial: bool = False, require_source_results: bool = False) -> None:
        now = _utc(now)
        if type(partial) is not bool or type(require_source_results) is not bool:
            raise ValueError("finish flags must be boolean")
        def op(state):
            nonlocal now
            now = self._guard_clock(state, now)
            job = state["jobs"].get(job_id)
            self._require_lease(job, owner, fence, now)
            if require_source_results:
                _, all_complete, _ = self._completed_job_sources(job)
                if not all_complete:
                    raise AdmissionDenied("SOURCE_RESULT_NOT_PERSISTED")
            job.update(state="PARTIAL" if partial else "COMPLETED", owner=None,
                       recovery_available=False,
                       continuation_available=False,
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
