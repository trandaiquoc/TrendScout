"""Fenced, expiring journal for normalized source results and recovery receipts."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.jobs.shared_cache import _safe_payload
from app.ports.atomic_state import CommitOutcomeUnknown

_MAX_INLINE_CHECKPOINT_BYTES = 150_000
_MAX_RESOLVED_CHECKPOINT_BYTES = 5_000_000
_MAX_CHECKPOINT_COMMIT_RETRIES = 3
_RECORD_PATHS = (
    ("progress", "wikimedia_receipt", "result", "records"),
    ("progress", "wikimedia_history_records"),
    ("progress", "google_trends_receipt", "result", "records"),
    ("progress", "collector", "records"),
    ("result", "records"),
)


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
    def __init__(self, admission: PublicAdmission, *, snapshot_blobs=None):
        if not isinstance(admission, PublicAdmission):
            raise TypeError("public admission boundary required")
        if snapshot_blobs is not None and not callable(getattr(snapshot_blobs, "put", None)):
            raise TypeError("snapshot blob store must support durable writes")
        self.admission = admission
        self.snapshot_blobs = snapshot_blobs

    def _authorize(self, source_id, grant, now):
        if (source_id not in self.admission.policy.enabled_sources
                or not isinstance(grant, CheckpointGrant) or grant.source_id != source_id
                or grant.allowed is not True or _utc(grant.expires_at) <= now):
            raise AdmissionDenied("CHECKPOINT_POLICY_UNAVAILABLE")

    @staticmethod
    def _at_path(value, path):
        current = value
        for component in path[:-1]:
            current = current.get(component) if isinstance(current, dict) else None
        return current.get(path[-1]) if isinstance(current, dict) else None

    @staticmethod
    def _set_path(value, path, replacement):
        current = value
        for component in path[:-1]:
            current = current.get(component) if isinstance(current, dict) else None
        if not isinstance(current, dict) or path[-1] not in current:
            raise ValueError("snapshot record path is invalid")
        current[path[-1]] = replacement

    def _resolve_snapshot(self, entry, *, source_id, data_mode, now):
        stored = entry.get("snapshot")
        if not isinstance(stored, dict):
            raise ValueError("invalid checkpoint snapshot")
        storage = entry.get("snapshot_storage", "INLINE")
        if storage == "EXTERNAL_RECORDS":
            reference = entry.get("snapshot_ref")
            if (self.snapshot_blobs is None or not isinstance(reference, dict)
                    or tuple(reference.get("path", ())) not in _RECORD_PATHS):
                raise ValueError("external checkpoint reference is invalid")
            snapshot = deepcopy(stored)
            records = self.snapshot_blobs.get(reference, now=now)
            self._set_path(snapshot, tuple(reference["path"]), records)
        elif storage == "INLINE" and entry.get("snapshot_ref") is None:
            snapshot = deepcopy(stored)
        else:
            raise ValueError("unknown checkpoint storage mode")

        if (snapshot.get("schema") != 1 or snapshot.get("source_id") != source_id
                or snapshot.get("data_mode") != data_mode
                or snapshot.get("plan_fingerprint") != self.admission.policy.plan_fingerprint):
            raise ValueError("invalid checkpoint scope")
        _safe_payload(snapshot)
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
        if (len(encoded) > _MAX_RESOLVED_CHECKPOINT_BYTES
                or hashlib.sha256(encoded).hexdigest() != entry.get("digest")):
            raise ValueError("checkpoint integrity check failed")
        return snapshot

    def _validate_existing_entry(self, entry, *, source_id, grant):
        if (entry.get("policy_version") != grant.policy_version
                or entry.get("data_mode") != grant.data_mode
                or type(entry.get("revision")) is not int or entry["revision"] < 1
                or not isinstance(entry.get("digest"), str) or len(entry["digest"]) != 64):
            raise ValueError("invalid checkpoint metadata")
        snapshot = entry.get("snapshot")
        if (not isinstance(snapshot, dict) or snapshot.get("schema") != 1
                or snapshot.get("source_id") != source_id
                or snapshot.get("data_mode") != grant.data_mode
                or snapshot.get("plan_fingerprint") != self.admission.policy.plan_fingerprint):
            raise ValueError("invalid checkpoint scope")
        storage = entry.get("snapshot_storage", "INLINE")
        if storage == "EXTERNAL_RECORDS":
            reference = entry.get("snapshot_ref")
            if (self.snapshot_blobs is None or not isinstance(reference, dict)
                    or tuple(reference.get("path", ())) not in _RECORD_PATHS
                    or not isinstance(reference.get("digest"), str)
                    or len(reference["digest"]) != 64
                    or type(reference.get("raw_bytes")) is not int
                    or type(reference.get("item_count")) is not int):
                raise ValueError("invalid external checkpoint reference")
            return
        if storage != "INLINE" or entry.get("snapshot_ref") is not None:
            raise ValueError("invalid inline checkpoint reference")
        _safe_payload(snapshot)
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
        if (len(encoded) > _MAX_INLINE_CHECKPOINT_BYTES
                or hashlib.sha256(encoded).hexdigest() != entry["digest"]):
            raise ValueError("invalid inline checkpoint integrity")

    def load(self, job_id, owner, fence, *, source_id, grant, now,
             claimed_checkpoints=None):
        now = _utc(now)
        self._authorize(source_id, grant, now)

        # claim_job already read and fenced the complete coordination document.
        # Reuse that immutable snapshot for the common read path instead of
        # starting another transaction against the same hot document. A later
        # checkpoint save still verifies the lease and fence transactionally.
        # Expired or tightened entries keep the original mutation path so their
        # retention changes and cleanup remain durable.
        if claimed_checkpoints is not None:
            if not isinstance(claimed_checkpoints, dict):
                raise AdmissionDenied("CHECKPOINT_INVALID")
            claimed_entry = claimed_checkpoints.get(source_id)
            if claimed_entry is None:
                return None
            if not isinstance(claimed_entry, dict):
                raise AdmissionDenied("CHECKPOINT_INVALID")
            if not claimed_entry:
                return None
            if (claimed_entry.get("policy_version") != grant.policy_version
                    or claimed_entry.get("data_mode") != grant.data_mode):
                raise AdmissionDenied("CHECKPOINT_POLICY_MISMATCH")
            try:
                if type(claimed_entry.get("revision")) is not int or claimed_entry["revision"] < 1:
                    raise ValueError("invalid checkpoint revision")
                claimed_snapshot = self._resolve_snapshot(
                    claimed_entry, source_id=source_id,
                    data_mode=grant.data_mode, now=now,
                )
                claimed_expiry = min(
                    _utc(datetime.fromisoformat(claimed_entry["retain_until"])),
                    _utc(datetime.fromisoformat(claimed_entry["created_at"]))
                    + timedelta(seconds=grant.max_retention_seconds),
                    _utc(grant.expires_at),
                )
                stored_expiry = _utc(datetime.fromisoformat(claimed_entry["retain_until"]))
            except AdmissionDenied:
                raise
            except (KeyError, TypeError, ValueError, UnicodeError):
                raise AdmissionDenied("CHECKPOINT_INVALID") from None
            if now < claimed_expiry and claimed_expiry == stored_expiry:
                return {"revision": claimed_entry["revision"],
                        "snapshot": claimed_snapshot,
                        "retain_until": claimed_expiry.isoformat()}

        def operation(job):
            entries = job.setdefault("source_checkpoints", {})
            entry = entries.get(source_id)
            if not entry:
                return None
            if (entry.get("policy_version") != grant.policy_version
                    or entry.get("data_mode") != grant.data_mode):
                raise AdmissionDenied("CHECKPOINT_POLICY_MISMATCH")
            try:
                if type(entry.get("revision")) is not int or entry["revision"] < 1:
                    raise ValueError("invalid checkpoint revision")
                snapshot = self._resolve_snapshot(entry, source_id=source_id,
                                                  data_mode=grant.data_mode, now=now)
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
            return {"revision": entry["revision"], "snapshot": snapshot,
                    "retain_until": expiry.isoformat()}

        return self.admission.transact_owned(job_id, owner, fence, now=now, operation=operation)

    def completed_result(self, job, *, source_id, grant, now):
        """Validate and return a terminal source result within the retention grant."""
        now = _utc(now)
        self._authorize(source_id, grant, now)
        if (not isinstance(job, dict) or job.get("state") not in {
                    "COMPLETED", "PARTIAL", "FAILED", "EXPIRED", "ABORTED",
                }
                or job.get("plan_fingerprint") != self.admission.policy.plan_fingerprint):
            return None
        entry = job.get("source_checkpoints", {}).get(source_id)
        if not isinstance(entry, dict):
            return None
        try:
            if (entry.get("policy_version") != grant.policy_version
                    or entry.get("data_mode") != grant.data_mode
                    or type(entry.get("revision")) is not int or entry["revision"] < 1):
                raise ValueError("invalid completed result metadata")
            snapshot = self._resolve_snapshot(entry, source_id=source_id,
                                              data_mode=grant.data_mode, now=now)
            if (snapshot.get("phase") != "COMPLETE"
                    or not isinstance(snapshot.get("result"), dict)
                    or snapshot["result"].get("data_mode") != grant.data_mode
                    or not isinstance(snapshot["result"].get("records"), list)
                    or type(snapshot["result"].get("record_count")) is not int
                    or snapshot["result"]["record_count"] != len(snapshot["result"]["records"])):
                raise ValueError("invalid completed result scope")
            created_at = _utc(datetime.fromisoformat(entry["created_at"]))
            expiry = min(_utc(datetime.fromisoformat(entry["retain_until"])),
                         created_at + timedelta(seconds=grant.max_retention_seconds),
                         _utc(grant.expires_at))
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise AdmissionDenied("CHECKPOINT_INVALID") from None
        if now >= expiry:
            return None
        return deepcopy(snapshot["result"])

    def save(self, job_id, owner, fence, *, source_id, grant, now,
             expected_revision, snapshot):
        now = _utc(now)
        return self.save_many(job_id, owner, fence, now=now, checkpoints=[{
            "source_id": source_id,
            "grant": grant,
            "expected_revision": expected_revision,
            "snapshot": snapshot,
        }])[source_id]

    def save_many(self, job_id, owner, fence, *, now, checkpoints):
        """Commit several terminal source receipts with one owned-state transaction."""
        now = _utc(now)
        if (not isinstance(checkpoints, list) or not checkpoints
                or len(checkpoints) > len(self.admission.policy.enabled_sources)):
            raise ValueError("a bounded checkpoint batch is required")
        prepared = []
        pending_blob_uploads = []
        seen = set()
        for item in checkpoints:
            if not isinstance(item, dict):
                raise ValueError("checkpoint batch entry must be an object")
            source_id = item.get("source_id")
            grant = item.get("grant")
            expected_revision = item.get("expected_revision")
            snapshot = item.get("snapshot")
            self._authorize(source_id, grant, now)
            if source_id in seen:
                raise ValueError("checkpoint batch contains a duplicate source")
            seen.add(source_id)
            if type(expected_revision) is not int or not 0 <= expected_revision <= 500:
                raise ValueError("invalid checkpoint revision")
            if (not isinstance(snapshot, dict) or snapshot.get("schema") != 1
                    or snapshot.get("source_id") != source_id
                    or snapshot.get("data_mode") != grant.data_mode
                    or snapshot.get("plan_fingerprint") != self.admission.policy.plan_fingerprint):
                raise ValueError("invalid checkpoint scope")
            _safe_payload(snapshot)
            try:
                serialized = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                                        allow_nan=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise ValueError("checkpoint must be finite JSON") from None
            if len(serialized) > _MAX_RESOLVED_CHECKPOINT_BYTES:
                raise ValueError("checkpoint exceeds bounded resolved storage budget")
            digest = hashlib.sha256(serialized).hexdigest()
            snapshot_copy = json.loads(serialized)
            snapshot_storage = "INLINE"
            if len(serialized) > _MAX_INLINE_CHECKPOINT_BYTES:
                if self.snapshot_blobs is None:
                    raise ValueError("checkpoint exceeds inline storage budget")
                record_path = next((path for path in _RECORD_PATHS
                                    if isinstance(self._at_path(snapshot_copy, path), list)), None)
                if record_path is None:
                    raise ValueError("large checkpoint has no supported source record list")
                records = self._at_path(snapshot_copy, record_path)
                self._set_path(snapshot_copy, record_path, [])
                pending_blob_uploads.append({
                    "records": records,
                    "now": now,
                    "retain_until": min(
                        _utc(grant.expires_at),
                        now + timedelta(seconds=grant.max_retention_seconds),
                    ),
                    "record_path": list(record_path),
                    "prepared_index": len(prepared),
                })
                snapshot_storage = "EXTERNAL_RECORDS"
                shell = json.dumps(snapshot_copy, sort_keys=True, ensure_ascii=False,
                                   allow_nan=False, separators=(",", ":")).encode("utf-8")
                if len(shell) > _MAX_INLINE_CHECKPOINT_BYTES:
                    raise ValueError("checkpoint metadata exceeds inline storage budget")
            prepared.append({
                "source_id": source_id,
                "grant": grant,
                "expected_revision": expected_revision,
                "digest": digest,
                "snapshot": snapshot_copy,
                "snapshot_ref": None,
                "snapshot_storage": snapshot_storage,
            })

        def upload_checkpoint_blob(item):
            reference = self.snapshot_blobs.put(
                item["records"], now=item["now"], retain_until=item["retain_until"],
            )
            return {**reference, "path": item["record_path"]}

        if pending_blob_uploads:
            if len(pending_blob_uploads) > 1:
                with ThreadPoolExecutor(
                    max_workers=min(4, len(pending_blob_uploads))
                ) as executor:
                    references = list(executor.map(upload_checkpoint_blob, pending_blob_uploads))
            else:
                references = [upload_checkpoint_blob(pending_blob_uploads[0])]
            for item, reference in zip(pending_blob_uploads, references, strict=True):
                prepared[item["prepared_index"]]["snapshot_ref"] = reference

        def operation(job):
            entries = job.setdefault("source_checkpoints", {})
            revisions = {}
            for item in prepared:
                source_id = item["source_id"]
                grant = item["grant"]
                expected_revision = item["expected_revision"]
                digest = item["digest"]
                existing = entries.get(source_id)
                # The job deadline bounds execution and recovery claims. A separate
                # retention grant controls how long an already completed result can
                # be read; tying both to job expiry made results disappear too early.
                expiry = min(_utc(grant.expires_at),
                             now + timedelta(seconds=grant.max_retention_seconds))
                if existing:
                    try:
                        self._validate_existing_entry(existing, source_id=source_id, grant=grant)
                        old_expiry = _utc(datetime.fromisoformat(existing["retain_until"]))
                        created_at = _utc(datetime.fromisoformat(existing["created_at"]))
                        revision = existing["revision"]
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
                    if (existing and revision == expected_revision + 1
                            and existing["digest"] == digest):
                        if expiry <= now:
                            raise AdmissionDenied("CHECKPOINT_EXPIRED")
                        existing["retain_until"] = expiry.isoformat()
                        revisions[source_id] = revision
                        continue
                    raise AdmissionDenied("CHECKPOINT_REVISION_CONFLICT")
                if expiry <= now:
                    raise AdmissionDenied("CHECKPOINT_EXPIRED")
                entry = {
                    "revision": revision + 1,
                    "digest": digest,
                    "data_mode": grant.data_mode,
                    "policy_version": grant.policy_version,
                    "created_at": existing["created_at"] if existing else now.isoformat(),
                    "retain_until": expiry.isoformat(),
                    "snapshot": item["snapshot"],
                    "snapshot_storage": item["snapshot_storage"],
                }
                if item["snapshot_ref"] is not None:
                    entry["snapshot_ref"] = item["snapshot_ref"]
                entries[source_id] = entry
                revisions[source_id] = revision + 1
            return revisions

        # A lost Firestore acknowledgement can safely retry the whole atomic
        # batch: each committed digest/revision is recognized as idempotent.
        for attempt in range(_MAX_CHECKPOINT_COMMIT_RETRIES):
            try:
                return self.admission.transact_owned(
                    job_id, owner, fence, now=now, operation=operation
                )
            except CommitOutcomeUnknown:
                if attempt + 1 == _MAX_CHECKPOINT_COMMIT_RETRIES:
                    raise
