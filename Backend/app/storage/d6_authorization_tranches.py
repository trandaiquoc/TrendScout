"""Offline-only schema and atomic accounting for a future D6.2 tranche.

This module deliberately has no provider, credential, transport, or launcher
dependency.  Its state key is fixed to the existing ``d6-smoke`` identity.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import socket
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from app.jobs.public_admission import SOURCE_POOLS, AdmissionDenied
from app.local_ledger import STATE_KEY
from app.sources.catalog import WIKIMEDIA_SOURCE_ID

LEGACY_APPROVAL_ID = "D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12"
STATE_SCHEMA = 2
FUTURE_REQUEST_CEILING = 2
LIFETIME_REQUEST_CEILING = 3
_HEX = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_AUTH_FIELDS = (
    "authorization_id", "source_id", "approval_id", "approver",
    "valid_from_utc", "expires_at_utc", "policy_version", "policy_digest",
    "query_plan_fingerprint", "endpoint_scope", "country_scope", "access_scope",
    "observation_date", "maximum_requests", "maximum_attempts",
    "maximum_job_starts", "maximum_cost_units", "maximum_monetary_fee",
    "currency", "processing_permission", "derived_metrics_permission",
    "retention_policy", "raw_body_policy", "security_controls", "rollback_owner",
    "authorization_status",
)


def _utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("UTC timestamp required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("absolute UTC timestamp required")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError("UTC timestamp required")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class AuthorizationTranche:
    authorization_id: str
    source_id: str
    approval_id: str
    approver: str
    valid_from_utc: str
    expires_at_utc: str
    policy_version: str
    policy_digest: str
    query_plan_fingerprint: str
    endpoint_scope: str
    country_scope: str
    access_scope: str
    observation_date: str
    maximum_requests: int
    maximum_attempts: int
    maximum_job_starts: int
    maximum_cost_units: int
    maximum_monetary_fee: str
    currency: str
    processing_permission: str
    derived_metrics_permission: str
    retention_policy: str
    raw_body_policy: str
    security_controls: str
    rollback_owner: str
    authorization_status: str = "INACTIVE"

    def validate(self) -> dict:
        value = asdict(self)
        if set(value) != set(_AUTH_FIELDS):
            raise ValueError("authorization contract fields mismatch")
        for name in _AUTH_FIELDS:
            if name in {"maximum_requests", "maximum_attempts", "maximum_job_starts", "maximum_cost_units"}:
                continue
            if type(value[name]) is not str or not value[name].strip():
                raise ValueError(f"authorization field missing: {name}")
        if (self.source_id != WIKIMEDIA_SOURCE_ID or self.approval_id == LEGACY_APPROVAL_ID
                or not _ID.fullmatch(self.authorization_id) or not _HEX.fullmatch(self.policy_digest)
                or not _HEX.fullmatch(self.query_plan_fingerprint)):
            raise ValueError("authorization identity or digest invalid")
        start, expiry = _utc(self.valid_from_utc), _utc(self.expires_at_utc)
        if start >= expiry:
            raise ValueError("authorization window invalid")
        if (type(self.maximum_requests) is not int or not 1 <= self.maximum_requests <= 2
                or type(self.maximum_attempts) is not int or not 1 <= self.maximum_attempts <= 2
                or type(self.maximum_job_starts) is not int or not 1 <= self.maximum_job_starts <= 2
                or type(self.maximum_cost_units) is not int or not 1 <= self.maximum_cost_units <= 2):
            raise ValueError("authorization budget exceeds technical ceiling")
        try:
            if date.fromisoformat(self.observation_date).isoformat() != self.observation_date:
                raise ValueError("observation date invalid")
            fee = Decimal(self.maximum_monetary_fee)
        except (ValueError, InvalidOperation):
            raise ValueError("fee or observation date invalid") from None
        if not fee.is_finite() or fee < 0 or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("fee or currency invalid")
        if self.authorization_status not in {"INACTIVE", "ACTIVE", "REVOKED", "EXPIRED"}:
            raise ValueError("authorization status invalid")
        if (self.authorization_status == "ACTIVE"
                and any(value.strip().upper() in {"PENDING", "UNKNOWN", "TBD"}
                        for name, value in asdict(self).items() if name not in {
                            "maximum_requests", "maximum_attempts", "maximum_job_starts",
                            "maximum_cost_units"})):
            raise ValueError("active authorization contains unresolved decisions")
        return value


def _canonical_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _validate_legacy(state: dict) -> dict:
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise AdmissionDenied("LEGACY_SCHEMA_UNSUPPORTED")
    required = {"schema", "policy_digest", "operational_enabled", "jobs", "keys",
                "starts", "quota", "inflight", "local_smoke_source_counts"}
    if not required <= set(state) or not set(state) <= required | {"last_observed_at"}:
        raise AdmissionDenied("LEGACY_LEDGER_INVALID")
    if type(state.get("operational_enabled")) is not bool:
        raise AdmissionDenied("LEGACY_SWITCH_INVALID")
    digest = state.get("policy_digest")
    if not isinstance(digest, str) or not _HEX.fullmatch(digest):
        raise AdmissionDenied("LEGACY_POLICY_DIGEST_INVALID")
    jobs = state.get("jobs")
    counts = state.get("local_smoke_source_counts")
    if (not isinstance(jobs, dict) or not isinstance(counts, dict)
            or not isinstance(state["keys"], dict) or not isinstance(state["inflight"], dict)):
        raise AdmissionDenied("LEGACY_LEDGER_INVALID")
    if "last_observed_at" in state:
        try:
            _utc(state["last_observed_at"])
        except (TypeError, ValueError):
            raise AdmissionDenied("LEGACY_LEDGER_INVALID") from None
    found = []
    for job_id, job in jobs.items():
        if not isinstance(job_id, str) or not isinstance(job, dict):
            raise AdmissionDenied("LEGACY_JOB_INVALID")
        receipts = job.get("receipts", {})
        if not isinstance(receipts, dict):
            raise AdmissionDenied("LEGACY_RECEIPT_INVALID")
        for receipt_id, receipt in receipts.items():
            if (not isinstance(receipt_id, str) or not _HEX.fullmatch(receipt_id)
                    or not isinstance(receipt, dict)
                    or not isinstance(receipt.get("source_id"), str)
                    or not isinstance(receipt.get("pool"), str)
                    or receipt.get("pool") != SOURCE_POOLS.get(receipt.get("source_id"))):
                raise AdmissionDenied("LEGACY_RECEIPT_INVALID")
            try:
                reserved = _utc(receipt["reserved_at"])
            except (KeyError, TypeError, ValueError):
                raise AdmissionDenied("LEGACY_RECEIPT_INVALID") from None
            found.append((job_id, receipt_id, receipt, reserved))
    wiki = [row for row in found if row[2]["source_id"] == WIKIMEDIA_SOURCE_ID]
    if len(wiki) != 1 or wiki[0][2]["pool"] != "wikimedia":
        raise AdmissionDenied("LEGACY_WIKIMEDIA_RECEIPT_INVALID")
    if counts.get(WIKIMEDIA_SOURCE_ID) != 1 or type(counts.get(WIKIMEDIA_SOURCE_ID)) is not int:
        raise AdmissionDenied("LEGACY_COUNTER_MISMATCH")
    if any(type(v) is not int or v != 1 for v in counts.values()):
        raise AdmissionDenied("LEGACY_COUNTER_MISMATCH")
    if set(counts) != {r[2]["source_id"] for r in found}:
        raise AdmissionDenied("LEGACY_COUNTER_MISMATCH")
    quota = state.get("quota")
    if not isinstance(quota, dict):
        raise AdmissionDenied("LEGACY_COUNTER_MISMATCH")
    reservation_time = wiki[0][3]
    expected_quota = {f"wikimedia:day:{reservation_time:%Y-%m-%d}",
                      f"wikimedia:month:{reservation_time:%Y-%m}"}
    wiki_entries = {k: v for k, v in quota.items()
                    if isinstance(k, str) and k.startswith("wikimedia:")}
    if set(wiki_entries) != expected_quota or any(not isinstance(v, dict) for v in wiki_entries.values()):
        raise AdmissionDenied("LEGACY_COUNTER_MISMATCH")
    if any(set(v) != {"requests", "cost"} or type(v.get("requests")) is not int
           or type(v.get("cost")) is not int or v["requests"] != 1 or v["cost"] != 1
           for v in wiki_entries.values()):
        raise AdmissionDenied("LEGACY_COUNTER_MISMATCH")
    starts = state.get("starts")
    if not isinstance(starts, list) or len(starts) != 1:
        raise AdmissionDenied("LEGACY_COUNTER_MISMATCH")
    try:
        _utc(starts[0])
    except (TypeError, ValueError):
        raise AdmissionDenied("LEGACY_COUNTER_MISMATCH") from None
    return {"job_id": wiki[0][0], "receipt_id": wiki[0][1],
            "receipt": copy.deepcopy(wiki[0][2]), "receipt_hash": _canonical_hash(wiki[0][2]),
            "reserved_at": wiki[0][3].isoformat(), "policy_digest": digest,
            "consumed_requests": 1, "consumed_cost_units": 1}


class SameLedgerTranches:
    """Atomic schema migration and append-only budget accounting on ``d6-smoke``."""

    def __init__(self, store, *, designated_host_id: str, host_id=None):
        if not callable(getattr(store, "transact", None)):
            raise TypeError("atomic state store required")
        if getattr(store, "data_mode", None) != "SYNTHETIC":
            raise ValueError("D6.2.5 tranche storage is restricted to synthetic/test stores")
        if not isinstance(designated_host_id, str) or not designated_host_id.strip():
            raise ValueError("designated canonical execution host required")
        self.store = store
        self.designated_host_id = designated_host_id
        self.host_id = host_id or socket.gethostname()

    def _host(self):
        if self.host_id != self.designated_host_id:
            raise AdmissionDenied("CANONICAL_EXECUTION_HOST_MISMATCH")

    def migrate_legacy(self, *, migrated_at_utc: str):
        """Migrate one validated v1 document atomically; never touch canonical state implicitly."""
        self._host()
        migrated_at = _utc(migrated_at_utc).isoformat()

        def operation(current):
            if not current:
                raise AdmissionDenied("LEGACY_LEDGER_MISSING")
            if current.get("schema") == STATE_SCHEMA:
                self._validate_v2(current)
                if current.get("designated_host_id") != self.designated_host_id:
                    raise AdmissionDenied("CANONICAL_EXECUTION_HOST_MISMATCH")
                return current, "ALREADY_MIGRATED"
            legacy = _validate_legacy(current)
            envelope = {
                "schema": STATE_SCHEMA, "state_identity": STATE_KEY,
                "designated_host_id": self.designated_host_id,
                "operational_enabled": False,
                "migration": {"from_schema": 1, "to_schema": 2,
                              "migrated_at_utc": migrated_at,
                              "source_state_sha256": _canonical_hash(current)},
                "legacy_state": copy.deepcopy(current),
                "legacy_tranche": {"approval_id": LEGACY_APPROVAL_ID,
                                   "source_id": WIKIMEDIA_SOURCE_ID,
                                   "receipt_id": legacy["receipt_id"],
                                   "receipt": legacy["receipt"],
                                   "receipt_sha256": legacy["receipt_hash"],
                                   "job_id": legacy["job_id"],
                                   "reserved_at_utc": legacy["reserved_at"],
                                   "policy_digest": legacy["policy_digest"],
                                   "consumed_requests": 1,
                                   "consumed_cost_units": 1,
                                   "state": "CLOSED_CONSUMED"},
                "technical_capacity": {"maximum_additional_requests": 2,
                                        "maximum_lifetime_requests": 3,
                                        "authorized_capacity_requests": 0,
                                        "state": "INACTIVE"},
                "authorizations": [], "reservations": [], "settlements": [],
                "job_starts": [],
            }
            self._validate_v2(envelope)
            return envelope, "MIGRATED"
        return self.store.transact(STATE_KEY, operation)


    @staticmethod
    def _validate_v2(state):
        if (not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA
                or state.get("state_identity") != STATE_KEY
                or state.get("operational_enabled") is not False
                or not isinstance(state.get("legacy_state"), dict)
                or not isinstance(state.get("designated_host_id"), str)
                or not isinstance(state.get("migration"), dict)
                or not isinstance(state.get("authorizations"), list)
                or not isinstance(state.get("reservations"), list)
                or not isinstance(state.get("settlements"), list)
                or not isinstance(state.get("job_starts"), list)):
            raise AdmissionDenied("TRANCHE_STATE_INVALID")
        runtime_state = state.get("runtime_admission_state", {})
        if not isinstance(runtime_state, dict):
            raise AdmissionDenied("TRANCHE_STATE_INVALID")
        legacy = _validate_legacy(state["legacy_state"])
        migration = state["migration"]
        if (migration.get("from_schema") != 1 or migration.get("to_schema") != 2
                or migration.get("source_state_sha256") != _canonical_hash(state["legacy_state"])):
            raise AdmissionDenied("TRANCHE_STATE_INVALID")
        try:
            _utc(migration["migrated_at_utc"])
        except (KeyError, TypeError, ValueError):
            raise AdmissionDenied("TRANCHE_STATE_INVALID") from None
        tranche = state.get("legacy_tranche", {})
        cap = state.get("technical_capacity", {})
        if (tranche.get("state") != "CLOSED_CONSUMED"
                or tranche.get("approval_id") != LEGACY_APPROVAL_ID
                or tranche.get("receipt_id") != legacy["receipt_id"]
                or tranche.get("receipt") != legacy["receipt"]
                or tranche.get("receipt_sha256") != legacy["receipt_hash"]
                or tranche.get("consumed_requests") != 1
                or tranche.get("consumed_cost_units") != 1
                or cap != {"maximum_additional_requests": 2, "maximum_lifetime_requests": 3,
                           "authorized_capacity_requests": 0, "state": "INACTIVE"}):
            raise AdmissionDenied("TRANCHE_STATE_INVALID")
        authorizations = {}
        for value in state["authorizations"]:
            try:
                auth = AuthorizationTranche(**value)
                checked = auth.validate()
            except (TypeError, ValueError):
                raise AdmissionDenied("TRANCHE_STATE_INVALID") from None
            auth_id = checked["authorization_id"]
            if auth_id in authorizations:
                raise AdmissionDenied("TRANCHE_STATE_INVALID")
            authorizations[auth_id] = checked
        reservations = state["reservations"]
        if len(reservations) > FUTURE_REQUEST_CEILING or 1 + len(reservations) > LIFETIME_REQUEST_CEILING:
            raise AdmissionDenied("TRANCHE_STATE_INVALID")
        reservation_ids, attempts, idempotencies = set(), set(), set()
        by_auth = {}
        for record in reservations:
            if not isinstance(record, dict):
                raise AdmissionDenied("TRANCHE_STATE_INVALID")
            try:
                auth_id = record["authorization_id"]
                attempt = record["attempt_id"]
                idem = record["idempotency_key"]
                reservation_id = record["reservation_id"]
                auth = authorizations[auth_id]
                valid = (
                    record["source_id"] == WIKIMEDIA_SOURCE_ID
                    and record["approval_id"] == auth["approval_id"]
                    and _ID.fullmatch(record["job_id"])
                    and _ID.fullmatch(attempt) and _ID.fullmatch(idem)
                    and reservation_id == hashlib.sha256(f"{auth_id}:{attempt}".encode()).hexdigest()
                    and type(record["cost_units"]) is int and record["cost_units"] > 0
                    and Decimal(record["monetary_fee"]).is_finite()
                    and Decimal(record["monetary_fee"]) >= 0
                    and record["outcome"] == "UNKNOWN"
                )
                _utc(record["reserved_at_utc"])
            except (KeyError, TypeError, ValueError, InvalidOperation):
                raise AdmissionDenied("TRANCHE_STATE_INVALID") from None
            if (not valid or reservation_id in reservation_ids or attempt in attempts
                    or idem in idempotencies):
                raise AdmissionDenied("TRANCHE_STATE_INVALID")
            reservation_ids.add(reservation_id)
            attempts.add(attempt)
            idempotencies.add(idem)
            by_auth.setdefault(auth_id, []).append(record)
        starts = state["job_starts"]
        expected_starts = {(r["authorization_id"], r["job_id"]) for r in reservations}
        if (len(starts) != len(reservations)
                or {(s.get("authorization_id"), s.get("job_id")) for s in starts
                    if isinstance(s, dict)} != expected_starts):
            raise AdmissionDenied("TRANCHE_STATE_INVALID")
        for auth_id, records in by_auth.items():
            auth = authorizations[auth_id]
            if (len(records) > min(2, auth["maximum_requests"], auth["maximum_attempts"])
                    or len(records) > auth["maximum_job_starts"]
                    or sum(r["cost_units"] for r in records) > auth["maximum_cost_units"]
                    or sum((Decimal(r["monetary_fee"]) for r in records), Decimal(0))
                    > Decimal(auth["maximum_monetary_fee"])):
                raise AdmissionDenied("TRANCHE_STATE_INVALID")
        settled = set()
        for settlement in state["settlements"]:
            if not isinstance(settlement, dict):
                raise AdmissionDenied("TRANCHE_STATE_INVALID")
            ref = settlement.get("reservation_id")
            if (ref not in reservation_ids or ref in settled
                    or not isinstance(settlement.get("outcome"), str)
                    or not settlement["outcome"]):
                raise AdmissionDenied("TRANCHE_STATE_INVALID")
            try:
                _utc(settlement["settled_at_utc"])
            except (KeyError, TypeError, ValueError):
                raise AdmissionDenied("TRANCHE_STATE_INVALID") from None
            settled.add(ref)
        return True

    def append_inactive_authorization(self, contract: AuthorizationTranche):
        """Record a complete but inactive contract. This API cannot activate a grant."""
        value = contract.validate()
        if value["authorization_status"] != "INACTIVE":
            raise AdmissionDenied("AUTHORIZATION_MUST_REMAIN_INACTIVE")
        self._host()
        def operation(state):
            self._validate_v2(state)
            if any(item.get("authorization_id") == value["authorization_id"] for item in state["authorizations"]):
                raise AdmissionDenied("AUTHORIZATION_ID_DUPLICATE")
            updated = copy.deepcopy(state)
            updated["authorizations"].append(value)
            return updated, "APPENDED_INACTIVE"
        return self.store.transact(STATE_KEY, operation)

    def reserve_attempt(self, *, authorization_id: str, approval_id: str, source_id: str,
                        policy_version: str, policy_digest: str, query_plan_fingerprint: str,
                        endpoint_scope: str, country_scope: str,
                        access_scope: str, observation_date: str, job_id: str, attempt_id: str,
                        idempotency_key: str, cost_units: int, monetary_fee: str,
                        currency: str, now_utc: str):
        """Reserve budget atomically. No dispatch interface is provided by this module."""
        self._host()
        now = _utc(now_utc)
        if any(not isinstance(v, str) or not _ID.fullmatch(v) for v in
               (authorization_id, approval_id, job_id, attempt_id, idempotency_key)):
            raise AdmissionDenied("ATTEMPT_IDENTITY_INVALID")
        if (type(cost_units) is not int or cost_units <= 0
                or not isinstance(monetary_fee, str)
                or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
            raise AdmissionDenied("COST_INVALID")
        try:
            fee = Decimal(monetary_fee)
        except InvalidOperation:
            raise AdmissionDenied("COST_INVALID") from None
        if not fee.is_finite() or fee < 0:
            raise AdmissionDenied("COST_INVALID")
        supplied_scope = (source_id, policy_version, policy_digest, query_plan_fingerprint, endpoint_scope,
                          country_scope, access_scope, observation_date)
        def operation(state):
            self._validate_v2(state)
            auths = [a for a in state["authorizations"] if a.get("authorization_id") == authorization_id]
            if len(auths) != 1:
                raise AdmissionDenied("AUTHORIZATION_MISSING")
            auth = AuthorizationTranche(**auths[0])
            contract = auth.validate()
            if contract["authorization_status"] != "ACTIVE":
                raise AdmissionDenied("AUTHORIZATION_INACTIVE")
            if approval_id == LEGACY_APPROVAL_ID or approval_id != contract["approval_id"]:
                raise AdmissionDenied("APPROVAL_IDENTITY_MISMATCH")
            expected_scope = tuple(contract[k] for k in
                                   ("source_id", "policy_version", "policy_digest",
                                    "query_plan_fingerprint", "endpoint_scope", "country_scope",
                                    "access_scope", "observation_date"))
            if supplied_scope != expected_scope:
                raise AdmissionDenied("AUTHORIZATION_SCOPE_MISMATCH")
            if currency != contract["currency"]:
                raise AdmissionDenied("AUTHORIZATION_CURRENCY_MISMATCH")
            if not _utc(contract["valid_from_utc"]) <= now < _utc(contract["expires_at_utc"]):
                raise AdmissionDenied("AUTHORIZATION_OUTSIDE_WINDOW")
            key = (attempt_id, idempotency_key)
            for previous in state["reservations"]:
                if previous["attempt_id"] == attempt_id or previous["idempotency_key"] == idempotency_key:
                    if (previous["attempt_id"], previous["idempotency_key"], previous["authorization_id"]) == (*key, authorization_id):
                        return state, "REPLAY"
                    raise AdmissionDenied("ATTEMPT_IDENTITY_CONFLICT")
            if any(r["authorization_id"] == authorization_id and r["job_id"] == job_id
                   for r in state["reservations"]):
                raise AdmissionDenied("JOB_ALREADY_STARTED")
            auth_reservations = [r for r in state["reservations"] if r["authorization_id"] == authorization_id]
            if len(auth_reservations) >= min(2, contract["maximum_requests"], contract["maximum_attempts"]):
                raise AdmissionDenied("TRANCHE_REQUEST_LIMIT")
            if sum(r["cost_units"] for r in auth_reservations) + cost_units > contract["maximum_cost_units"]:
                raise AdmissionDenied("TRANCHE_COST_LIMIT")
            reserved_fees = sum((Decimal(r["monetary_fee"]) for r in auth_reservations), Decimal(0))
            if reserved_fees + fee > Decimal(contract["maximum_monetary_fee"]):
                raise AdmissionDenied("TRANCHE_MONETARY_FEE_LIMIT")
            if len([s for s in state["job_starts"] if s["authorization_id"] == authorization_id]) >= contract["maximum_job_starts"]:
                raise AdmissionDenied("TRANCHE_JOB_START_LIMIT")
            total = 1 + len(state["reservations"])
            if total >= 3:
                raise AdmissionDenied("LIFETIME_REQUEST_LIMIT")
            if len([r for r in state["reservations"] if r["authorization_id"] == authorization_id
                    and not any(s["reservation_id"] == r["reservation_id"] for s in state["settlements"])]) >= 1:
                raise AdmissionDenied("TRANCHE_CONCURRENCY_LIMIT")
            updated = copy.deepcopy(state)
            record = {"reservation_id": hashlib.sha256(f"{authorization_id}:{attempt_id}".encode()).hexdigest(),
                      "authorization_id": authorization_id, "approval_id": approval_id,
                      "source_id": source_id, "job_id": job_id, "attempt_id": attempt_id,
                      "idempotency_key": idempotency_key, "cost_units": cost_units,
                      "monetary_fee": str(fee),
                      "reserved_at_utc": now.isoformat(), "outcome": "UNKNOWN"}
            updated["reservations"].append(record)
            updated["job_starts"].append({"authorization_id": authorization_id,
                                           "job_id": job_id, "started_at_utc": now.isoformat()})
            return updated, "NEW"
        return self.store.transact(STATE_KEY, operation)

    def settle_attempt(self, *, reservation_id: str, outcome: str, now_utc: str):
        self._host()
        now = _utc(now_utc)
        if not _HEX.fullmatch(reservation_id) or not isinstance(outcome, str) or not outcome:
            raise AdmissionDenied("SETTLEMENT_INVALID")
        def operation(state):
            self._validate_v2(state)
            if not any(r["reservation_id"] == reservation_id for r in state["reservations"]):
                raise AdmissionDenied("RESERVATION_MISSING")
            if any(s["reservation_id"] == reservation_id for s in state["settlements"]):
                return state, "REPLAY"
            updated = copy.deepcopy(state)
            updated["settlements"].append({"reservation_id": reservation_id,
                                           "outcome": outcome, "settled_at_utc": now.isoformat()})
            return updated, "SETTLED"
        return self.store.transact(STATE_KEY, operation)


def read_only_ledger_compatibility_preflight(path, *, designated_host_id: str,
                                             host_id: str) -> dict:
    """Validate a later canonical ledger without opening it writable or returning state.

    This helper is intended for the separately approved canonical host. Current
    offline acceptance calls it only with representative temporary databases.
    """
    if (not isinstance(designated_host_id, str) or not designated_host_id.strip()
            or not isinstance(host_id, str) or not host_id.strip()):
        return {"status": "BLOCKED", "reason": "HOST_ID_REQUIRED"}
    if host_id != designated_host_id:
        return {"status": "BLOCKED", "reason": "CANONICAL_EXECUTION_HOST_MISMATCH"}
    try:
        ledger_path = Path(path).expanduser().absolute()
        if not ledger_path.is_file() or ledger_path.is_symlink():
            return {"status": "BLOCKED", "reason": "LEDGER_UNAVAILABLE"}
        uri = f"file:{quote(str(ledger_path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "atomic_state" not in tables:
                return {"status": "BLOCKED", "reason": "LEDGER_SCHEMA_UNSUPPORTED"}
            row = connection.execute(
                "SELECT state_json FROM atomic_state WHERE state_key = ?", (STATE_KEY,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return {"status": "BLOCKED", "reason": "LEDGER_STATE_MISSING"}
        state = json.loads(row[0])
        if not isinstance(state, dict):
            return {"status": "BLOCKED", "reason": "LEDGER_VALIDATION_FAILED"}
        if state.get("schema") == 1:
            _validate_legacy(state)
            return {"status": "READ_ONLY_COMPATIBLE", "schema": 1,
                    "reason": "VALIDATED_V1_REQUIRES_SEPARATE_MIGRATION_AUTHORIZATION"}
        if state.get("schema") == STATE_SCHEMA:
            SameLedgerTranches._validate_v2(state)
            if state.get("designated_host_id") != designated_host_id:
                return {"status": "BLOCKED", "reason": "CANONICAL_EXECUTION_HOST_MISMATCH"}
            return {"status": "READ_ONLY_COMPATIBLE", "schema": STATE_SCHEMA,
                    "reason": "VALIDATED_V2_NO_MUTATION_PERFORMED"}
        return {"status": "BLOCKED", "reason": "LEDGER_SCHEMA_UNSUPPORTED"}
    except (OSError, sqlite3.Error, json.JSONDecodeError, AdmissionDenied,
            TypeError, ValueError):
        return {"status": "BLOCKED", "reason": "LEDGER_VALIDATION_FAILED"}
