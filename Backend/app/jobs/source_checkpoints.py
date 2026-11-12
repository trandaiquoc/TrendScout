"""Fenced checkpoint journal for normalized source data; not runtime-wired.

The caller must supply a current, server-owned retention grant on every read and
write. A production grant provider and proactive deletion remain unimplemented.
"""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.shared_cache import _safe_payload

_MAX_CHECKPOINT_BYTES = 150_000


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CheckpointGrant:
    source_id: str
    data_mode: str
    policy_version: str
    expires_at: datetime
    max_retention_seconds: int
    allowed: bool = False

    def __post_init__(self):
        _utc(self.expires_at)
        if (not isinstance(self.source_id, str) or not self.source_id
                or self.data_mode not in {"SYNTHETIC", "LIVE"}
                or not isinstance(self.policy_version, str) or not self.policy_version.strip()
                or type(self.allowed) is not bool or type(self.max_retention_seconds) is not int
                or not 1 <= self.max_retention_seconds <= 86_400):
            raise ValueError("invalid checkpoint retention grant")


class SourceCheckpointJournal:
    def __init__(self, admission: PublicAdmission):
        if not isinstance(admission, PublicAdmission):
            raise TypeError("public admission boundary required")
        self.admission = admission

    def _authorize(self, source_id, grant, now):
        if (source_id not in self.admission.policy.enabled_sources
                or not isinstance(grant, CheckpointGrant) or grant.source_id != source_id
                or grant.allowed is not True or _utc(grant.expires_at) <= now):
            raise AdmissionDenied("CHECKPOINT_POLICY_UNAVAILABLE")

    def load(self, job_id, owner, fence, *, source_id, grant, now):
        now = _utc(now)
        self._authorize(source_id, grant, now)
        def operation(job):
            entries = job.setdefault("source_checkpoints", {})
            entry = entries.get(source_id)
            if not entry:
                return None
            if (entry.get("policy_version") != grant.policy_version
                    or entry.get("data_mode") != grant.data_mode):
                raise AdmissionDenied("CHECKPOINT_POLICY_MISMATCH")
            try:
                if (type(entry.get("revision")) is not int or entry["revision"] < 1
                        or not isinstance(entry.get("snapshot"), dict)
                        or entry["snapshot"].get("schema") != 1
                        or entry["snapshot"].get("source_id") != source_id
                        or entry["snapshot"].get("data_mode") != grant.data_mode
                        or entry["snapshot"].get("plan_fingerprint") != self.admission.policy.plan_fingerprint):
                    raise ValueError("invalid checkpoint scope")
                _safe_payload(entry["snapshot"])
                encoded = json.dumps(entry["snapshot"], sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode("utf-8")
                if (len(encoded) > _MAX_CHECKPOINT_BYTES
                        or hashlib.sha256(encoded).hexdigest() != entry["digest"]):
                    raise ValueError("invalid digest")
                expiry = min(_utc(datetime.fromisoformat(entry["retain_until"])),
                             _utc(datetime.fromisoformat(entry["created_at"]))
                             + timedelta(seconds=grant.max_retention_seconds),
                             _utc(grant.expires_at))
            except (KeyError, TypeError, ValueError, UnicodeError):
                raise AdmissionDenied("CHECKPOINT_INVALID") from None
            if now >= expiry:
                del entries[source_id]
                return None
            entry["retain_until"] = expiry.isoformat()
            return {"revision": entry["revision"], "snapshot": deepcopy(entry["snapshot"]),
                    "retain_until": expiry.isoformat()}
        return self.admission.transact_owned(job_id, owner, fence, now=now, operation=operation)

    def save(self, job_id, owner, fence, *, source_id, grant, now,
             expected_revision, snapshot):
        now = _utc(now)
        self._authorize(source_id, grant, now)
        if type(expected_revision) is not int or not 0 <= expected_revision <= 500:
            raise ValueError("invalid checkpoint revision")
        if (not isinstance(snapshot, dict) or snapshot.get("schema") != 1
                or snapshot.get("source_id") != source_id
                or snapshot.get("data_mode") != grant.data_mode
                or snapshot.get("plan_fingerprint") != self.admission.policy.plan_fingerprint):
            raise ValueError("invalid checkpoint scope")
        _safe_payload(snapshot)
        try:
            serialized = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("checkpoint must be finite JSON") from None
        if len(serialized) > _MAX_CHECKPOINT_BYTES:
            raise ValueError("checkpoint exceeds storage budget")
        snapshot_copy = json.loads(serialized)
        digest = hashlib.sha256(serialized).hexdigest()
        def operation(job):
            entries = job.setdefault("source_checkpoints", {})
            existing = entries.get(source_id)
            expiry = min(_utc(grant.expires_at), now + timedelta(seconds=grant.max_retention_seconds),
                         _utc(datetime.fromisoformat(job["expires_at"])))
            if existing:
                try:
                    if (existing.get("policy_version") != grant.policy_version
                            or existing.get("data_mode") != grant.data_mode):
                        raise AdmissionDenied("CHECKPOINT_POLICY_MISMATCH")
                    old_expiry = _utc(datetime.fromisoformat(existing["retain_until"]))
                    created_at = _utc(datetime.fromisoformat(existing["created_at"]))
                    revision = existing["revision"]
                    if (type(revision) is not int or revision < 1
                            or hashlib.sha256(json.dumps(existing["snapshot"], sort_keys=True,
                                ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                                .encode("utf-8")).hexdigest() != existing["digest"]):
                        raise ValueError("invalid checkpoint entry")
                    _safe_payload(existing["snapshot"])
                except AdmissionDenied:
                    raise
                except (KeyError, TypeError, ValueError, UnicodeError):
                    raise AdmissionDenied("CHECKPOINT_INVALID") from None
                if now >= old_expiry:
                    raise AdmissionDenied("CHECKPOINT_EXPIRED")
                expiry = min(expiry, old_expiry,
                             created_at + timedelta(seconds=grant.max_retention_seconds))
            else:
                revision = 0
            if revision != expected_revision:
                if existing and revision == expected_revision + 1 and existing["digest"] == digest:
                    if expiry <= now:
                        raise AdmissionDenied("CHECKPOINT_EXPIRED")
                    existing["retain_until"] = expiry.isoformat()
                    return revision
                raise AdmissionDenied("CHECKPOINT_REVISION_CONFLICT")
            if expiry <= now:
                raise AdmissionDenied("CHECKPOINT_EXPIRED")
            entries[source_id] = {"revision": revision + 1, "digest": digest,
                "data_mode": grant.data_mode, "policy_version": grant.policy_version,
                "created_at": existing["created_at"] if existing else now.isoformat(),
                "retain_until": expiry.isoformat(), "snapshot": snapshot_copy}
            return revision + 1
        return self.admission.transact_owned(job_id, owner, fence, now=now, operation=operation)
