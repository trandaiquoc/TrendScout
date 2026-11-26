"""Capture and stage legacy Firestore coordination without changing authority.

This utility does not fence Firestore writers or perform cutover. Its staged
data must be recaptured and reconciled after an independently verified fence.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from psycopg import sql

from app.storage.document_security import validate_no_credentials
from app.storage.firestore_snapshot_blobs import FirestoreSnapshotBlobs, _manifest
from app.storage.postgres_atomic_state import PostgresAtomicStateStore, _same, _valid_key, _validate
from app.storage.postgres_config import verify_same_database
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import verify_migrations
from app.storage.postgres_payloads import PostgresSourcePayloads, _encode, _valid_reference

_POINTER = "collections-live-snapshot-v1"


class LegacyCaptureError(ValueError):
    """Legacy data cannot be staged safely or changed during capture."""


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LegacyCaptureError("timezone-aware capture time required")
    return value.astimezone(timezone.utc)


def _fingerprint(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise LegacyCaptureError("legacy document is not finite UTF-8 JSON") from None
    return hashlib.sha256(raw).hexdigest()


def _scan_sensitive(value):
    try:
        validate_no_credentials(value)
    except ValueError as exc:
        raise LegacyCaptureError(str(exc)) from None


def _references(value, output):
    if isinstance(value, dict):
        if {"digest", "raw_bytes", "item_count"} <= value.keys():
            if not _valid_reference(value):
                raise LegacyCaptureError("legacy source payload reference is invalid")
            old = output.setdefault(value["digest"], dict(value))
            if old != value:
                raise LegacyCaptureError("legacy source payload references conflict")
            return
        for child in value.values():
            _references(child, output)
    elif isinstance(value, list):
        for child in value:
            _references(child, output)


def _read_documents(client):
    result = {}
    for snapshot in client.collection("_ts_d5_coordination").stream(timeout=30, retry=None):
        if not _valid_key(snapshot.id) or snapshot.id in result:
            raise LegacyCaptureError("legacy coordination document identifier is invalid")
        value = snapshot.to_dict()
        if not isinstance(value, dict):
            raise LegacyCaptureError("legacy coordination document is invalid")
        result[snapshot.id] = value
    if _POINTER not in result:
        raise LegacyCaptureError("legacy Collections snapshot pointer is missing")
    return result


def _ensure_no_active_leases(documents, now):
    for document in documents.values():
        for job in document.get("jobs", {}).values() if isinstance(document.get("jobs"), dict) else ():
            if not isinstance(job, dict):
                raise LegacyCaptureError("legacy job metadata is invalid")
            value = job.get("lease_until")
            if job.get("state") == "RUNNING" and not value:
                raise LegacyCaptureError("running legacy job has no lease deadline")
            if value and _parse_time(value) > now:
                raise LegacyCaptureError("active legacy worker lease requires reconciliation")
        for reservation in document.get("inflight", {}).values() if isinstance(document.get("inflight"), dict) else ():
            if not isinstance(reservation, dict) or "expires_at" not in reservation:
                raise LegacyCaptureError("legacy reservation metadata is invalid")
            if _parse_time(reservation["expires_at"]) > now:
                raise LegacyCaptureError("active legacy reservation requires reconciliation")


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _utc(parsed)
    except (AttributeError, TypeError, ValueError):
        raise LegacyCaptureError("legacy timestamp is invalid") from None


@dataclass(frozen=True)
class LegacyCapture:
    captured_at: datetime
    documents: dict = field(repr=False)
    fingerprints: dict = field(repr=False)
    payloads: tuple = field(repr=False)  # (reference, expiry, records or None if expired)
    expired_references: int = 0


def capture_firestore_coordination(client, *, now, blob_reader=None):
    """Read and verify the current legacy documents and unexpired blobs."""
    now = _utc(now)
    documents = _read_documents(client)
    references = {}
    fingerprints = {}
    for key, value in documents.items():
        fingerprints[key] = _fingerprint(value)
        if key != _POINTER:
            _validate(value)
            _scan_sensitive(value)
            _references(value, references)
    blob_reader = blob_reader or FirestoreSnapshotBlobs(client)
    payloads, expired = [], 0
    for digest, reference in sorted(references.items()):
        snapshot = client.collection("_ts_d5_cache_blobs").document(digest).get(timeout=30, retry=None)
        try:
            manifest = _manifest(snapshot, digest)
        except ValueError:
            raise LegacyCaptureError("legacy source payload manifest is invalid") from None
        if (manifest["raw_bytes"] != reference["raw_bytes"]
                or manifest["item_count"] != reference["item_count"]):
            raise LegacyCaptureError("legacy source payload metadata conflicts")
        expiry = manifest["expiry"]
        if expiry <= now:
            expired += 1
            payloads.append((deepcopy(reference), expiry, None))
            continue
        records = blob_reader.get(reference, now=now)
        _scan_sensitive(records)
        raw = _encode(records)
        if (hashlib.sha256(raw).hexdigest() != digest or len(raw) != reference["raw_bytes"]
                or len(records) != reference["item_count"]):
            raise LegacyCaptureError("legacy source payload cannot be reproduced exactly")
        payloads.append((deepcopy(reference), expiry, records))
    return LegacyCapture(now, deepcopy(documents), fingerprints, tuple(payloads), expired)


def stage_firestore_coordination(client, environment, capture, *, schema, now,
                                 clock=lambda: datetime.now(timezone.utc)):
    """Stage exact state idempotently; this function never performs cutover.

    All document fingerprints are compared again before staging. This is not a
    substitute for disabling/fencing legacy writers at final cutover.
    """
    now = _utc(now)
    if not isinstance(capture, LegacyCapture) or now < capture.captured_at:
        raise LegacyCaptureError("invalid or time-regressed legacy capture")
    observed_now = _utc(clock())
    if observed_now < now:
        raise LegacyCaptureError("legacy staging clock regressed")
    now = observed_now
    current = _read_documents(client)
    if ({key: _fingerprint(value) for key, value in current.items()} != capture.fingerprints
            or {key: _fingerprint(value) for key, value in capture.documents.items()} != capture.fingerprints):
        raise LegacyCaptureError("legacy coordination changed after capture")
    _ensure_no_active_leases(current, now)
    for reference, expiry, records in capture.payloads:
        snapshot = client.collection("_ts_d5_cache_blobs").document(reference["digest"]).get(
            timeout=30, retry=None
        )
        try:
            manifest = _manifest(snapshot, reference["digest"])
        except ValueError:
            raise LegacyCaptureError("legacy source payload manifest changed or is invalid") from None
        if (manifest["expiry"] != expiry or manifest["raw_bytes"] != reference["raw_bytes"]
                or manifest["item_count"] != reference["item_count"]):
            raise LegacyCaptureError("legacy source payload manifest changed after capture")
        if records is not None and expiry <= now:
            raise LegacyCaptureError("legacy source payload expired after capture")
    verify_same_database(environment)
    store = PostgresAtomicStateStore(environment, schema=schema)
    payload_store = PostgresSourcePayloads(environment, schema=schema)
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        verify_migrations(connection, schema=schema)
        for reference, expiry, records in capture.payloads:
            if records is not None:
                actual_now = _utc(clock())
                if actual_now < now or expiry <= actual_now:
                    raise LegacyCaptureError("legacy source payload expired during staging")
                now = actual_now
                payload_store.import_legacy(reference, records, now=actual_now, retain_until=expiry)
        for key, value in capture.documents.items():
            if key != _POINTER:
                def import_state(existing):
                    if existing and not _same(existing, value):
                        raise LegacyCaptureError("staged coordination conflicts with legacy state")
                    return deepcopy(value), None
                store.transact(key, import_state)
            disposition = "SQLITE_POINTER" if key == _POINTER else "COORDINATION"
            query = sql.SQL("""INSERT INTO {}.legacy_firestore_imports
                (document_id,content_sha256,disposition,captured_at) VALUES (%s,%s,%s,%s)
                ON CONFLICT(document_id) DO NOTHING""").format(sql.Identifier(schema))
            with connection.transaction():
                connection.execute(query, (key, capture.fingerprints[key], disposition, capture.captured_at))
                row = connection.execute(sql.SQL("SELECT content_sha256,disposition FROM {}.legacy_firestore_imports "
                                                 "WHERE document_id=%s").format(sql.Identifier(schema)),
                                         (key,)).fetchone()
                if row != (capture.fingerprints[key], disposition):
                    raise LegacyCaptureError("legacy import audit conflicts with staged state")
        final_documents = _read_documents(client)
        if {key: _fingerprint(value) for key, value in final_documents.items()} != capture.fingerprints:
            raise LegacyCaptureError("legacy coordination changed during staging")
        state_keys = [key for key in capture.documents if key != _POINTER]
        staged = store.read_many(state_keys) if state_keys else {}
        if any(not _same(staged[key], capture.documents[key]) for key in state_keys):
            raise LegacyCaptureError("staged coordination failed semantic reconciliation")
        final_now = _utc(clock())
        if final_now < now:
            raise LegacyCaptureError("legacy staging clock regressed")
        for reference, expiry, records in capture.payloads:
            snapshot = client.collection("_ts_d5_cache_blobs").document(reference["digest"]).get(
                timeout=30, retry=None
            )
            try:
                manifest = _manifest(snapshot, reference["digest"])
            except ValueError:
                raise LegacyCaptureError("legacy source payload manifest changed or is invalid") from None
            if (manifest["expiry"] != expiry or manifest["raw_bytes"] != reference["raw_bytes"]
                    or manifest["item_count"] != reference["item_count"]):
                raise LegacyCaptureError("legacy source payload manifest changed during staging")
            if records is not None:
                if expiry <= final_now or not _same(payload_store.get(reference, now=final_now), records):
                    raise LegacyCaptureError("staged source payload failed reconciliation")
    return {"documents": len(capture.documents),
            "payloads": sum(records is not None for _, _, records in capture.payloads),
            "expired_payload_references": capture.expired_references}
