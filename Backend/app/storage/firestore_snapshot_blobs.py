"""Bounded, content-addressed JSON snapshots split across Firestore documents.

The client is injected by the caller. A manifest and every chunk are written
inside one Firestore transaction, so readers cannot observe a partial blob.
Production Vercel Shared LIVE and the local opt-in runtime use this adapter.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from threading import Lock

from google.cloud import firestore

_COLLECTION = "_ts_d5_cache_blobs"
_MAX_BLOB_BYTES = 5_700_000
_MAX_ITEMS = 5000
_CHUNK_BYTES = 196_608
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_logger = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware snapshot timestamp required")
    return value.astimezone(timezone.utc)


def _manifest(snapshot, digest: str) -> dict:
    value = snapshot.to_dict() if snapshot.exists else None
    if (not isinstance(value, dict) or type(value.get("schema")) is not int
            or value.get("schema") != 1
            or value.get("digest") != digest
            or type(value.get("raw_bytes")) is not int
            or not 0 <= value["raw_bytes"] <= _MAX_BLOB_BYTES
            or type(value.get("item_count")) is not int
            or not 0 <= value["item_count"] <= _MAX_ITEMS
            or type(value.get("chunk_count")) is not int
            or value["chunk_count"] != max(1, (value["raw_bytes"] + _CHUNK_BYTES - 1) // _CHUNK_BYTES)
            or not isinstance(value.get("retain_until"), str)):
        raise ValueError("stored Firestore snapshot manifest is invalid")
    try:
        expiry = _utc(datetime.fromisoformat(value["retain_until"]))
    except (TypeError, ValueError):
        raise ValueError("stored Firestore snapshot retention is invalid") from None
    return {**value, "expiry": expiry}


class FirestoreSnapshotBlobs:
    """Store finite JSON lists in bounded chunks with atomic manifest visibility."""

    def __init__(self, client, *, max_attempts: int = 5, read_cache=None):
        if not callable(getattr(client, "transaction", None)) or not callable(
            getattr(client, "collection", None)
        ):
            raise TypeError("injected Firestore client required")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("bounded transaction retries required")
        if read_cache is not None and not isinstance(read_cache, _SnapshotReadCache):
            raise TypeError("bounded verified snapshot read cache required")
        self._client, self._max_attempts = client, max_attempts
        self._read_cache = read_cache

    def _manifest_ref(self, digest: str):
        return self._client.collection(_COLLECTION).document(digest)

    def _chunk_ref(self, digest: str, index: int):
        return self._manifest_ref(digest).collection("chunks").document(f"{index:08d}")

    def put(self, records, *, now: datetime, retain_until: datetime) -> dict:
        now, retain_until = _utc(now), _utc(retain_until)
        if (not isinstance(records, list) or len(records) > _MAX_ITEMS
                or retain_until <= now):
            raise ValueError("bounded records and a future retention deadline are required")
        try:
            raw = json.dumps(records, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("snapshot records must be finite UTF-8 JSON") from None
        if len(raw) > _MAX_BLOB_BYTES:
            raise ValueError("snapshot record blob exceeds its bounded storage budget")

        digest = hashlib.sha256(raw).hexdigest()
        chunks = [raw[offset:offset + _CHUNK_BYTES]
                  for offset in range(0, len(raw), _CHUNK_BYTES)] or [b""]
        manifest_ref = self._manifest_ref(digest)
        chunk_refs = [self._chunk_ref(digest, index) for index in range(len(chunks))]
        transaction = self._client.transaction(max_attempts=self._max_attempts)

        def write_snapshot(tx):
            snapshot = next(iter(tx.get(manifest_ref)))
            effective_expiry = retain_until
            if snapshot.exists:
                previous = _manifest(snapshot, digest)
                if (previous["raw_bytes"] != len(raw)
                        or previous["item_count"] != len(records)
                        or previous["chunk_count"] != len(chunks)):
                    raise ValueError("snapshot digest collision or invalid manifest")
                # A shared digest can be referenced by several cache scopes;
                # storage must outlive every live reference to those bytes.
                effective_expiry = max(previous["expiry"], retain_until)
            retention_text = effective_expiry.isoformat()
            for index, (reference, content) in enumerate(zip(chunk_refs, chunks, strict=True)):
                tx.set(reference, {
                    "schema": 1,
                    "digest": digest,
                    "index": index,
                    "data_b64": base64.b64encode(content).decode("ascii"),
                    "retain_until": retention_text,
                })
            tx.set(manifest_ref, {
                "schema": 1,
                "digest": digest,
                "raw_bytes": len(raw),
                "item_count": len(records),
                "chunk_count": len(chunks),
                "retain_until": retention_text,
            })

        firestore.transactional(write_snapshot)(transaction)
        return {"digest": digest, "raw_bytes": len(raw), "item_count": len(records)}

    def get(self, reference, *, now: datetime) -> list:
        now = _utc(now)
        if (not isinstance(reference, dict)
                or not isinstance(reference.get("digest"), str)
                or not _DIGEST.fullmatch(reference["digest"])
                or type(reference.get("raw_bytes")) is not int
                or not 0 <= reference["raw_bytes"] <= _MAX_BLOB_BYTES
                or type(reference.get("item_count")) is not int
                or not 0 <= reference["item_count"] <= _MAX_ITEMS):
            raise ValueError("invalid snapshot blob reference")

        digest = reference["digest"]
        snapshot = self._manifest_ref(digest).get()
        try:
            manifest = _manifest(snapshot, digest)
        except ValueError:
            if self._read_cache is not None:
                self._read_cache.discard(digest)
            raise
        if (manifest["raw_bytes"] != reference["raw_bytes"]
                or manifest["item_count"] != reference["item_count"]):
            raise ValueError("snapshot blob reference does not match stored metadata")
        if manifest["expiry"] <= now:
            if self._read_cache is not None:
                self._read_cache.discard(digest)
            raise ValueError("snapshot blob is expired")

        raw = (self._read_cache.get(
            digest, raw_bytes=manifest["raw_bytes"], item_count=manifest["item_count"]
        ) if self._read_cache is not None else None)
        cache_hit = raw is not None
        if raw is None:
            chunk_refs = [
                self._chunk_ref(digest, index) for index in range(manifest["chunk_count"])
            ]
            chunk_snapshots = {}
            for chunk in self._client.get_all(chunk_refs):
                chunk_id = getattr(chunk, "id", None)
                if not isinstance(chunk_id, str) or chunk_id in chunk_snapshots:
                    raise ValueError("snapshot blob chunk is missing or invalid")
                chunk_snapshots[chunk_id] = chunk

            pieces = []
            byte_count = 0
            for index in range(manifest["chunk_count"]):
                chunk = chunk_snapshots.get(f"{index:08d}")
                value = chunk.to_dict() if chunk is not None and chunk.exists else None
                if (not isinstance(value, dict) or type(value.get("schema")) is not int
                        or value.get("schema") != 1 or value.get("digest") != digest
                        or type(value.get("index")) is not int or value.get("index") != index
                        or value.get("retain_until") != manifest["retain_until"]
                        or not isinstance(value.get("data_b64"), str)):
                    raise ValueError("snapshot blob chunk is missing or invalid")
                try:
                    piece = base64.b64decode(value["data_b64"], validate=True)
                except (binascii.Error, ValueError):
                    raise ValueError("snapshot blob chunk encoding is invalid") from None
                if len(piece) > _CHUNK_BYTES or byte_count + len(piece) > _MAX_BLOB_BYTES:
                    raise ValueError("snapshot blob chunk exceeds its bounded size")
                pieces.append(piece)
                byte_count += len(piece)
            raw = b"".join(pieces)

        if (len(raw) != manifest["raw_bytes"]
                or hashlib.sha256(raw).hexdigest() != digest):
            if cache_hit and self._read_cache is not None:
                self._read_cache.discard(digest)
            raise ValueError("snapshot blob integrity check failed")
        try:
            records = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if cache_hit and self._read_cache is not None:
                self._read_cache.discard(digest)
            raise ValueError("snapshot blob JSON is invalid") from None
        if not isinstance(records, list) or len(records) != manifest["item_count"]:
            if cache_hit and self._read_cache is not None:
                self._read_cache.discard(digest)
            raise ValueError("snapshot blob record count is invalid")
        if self._read_cache is not None:
            if cache_hit:
                _logger.info("Firestore snapshot read cache=HIT raw_bytes=%d", len(raw))
            else:
                self._read_cache.put(
                    digest, raw_bytes=manifest["raw_bytes"],
                    item_count=manifest["item_count"], raw=raw,
                )
                _logger.info("Firestore snapshot read cache=MISS raw_bytes=%d", len(raw))
        return records

    def purge_expired(self, *, now: datetime) -> int:
        """Atomically remove expired manifests and all of their content chunks."""
        now = _utc(now)
        candidates = self._client.collection(_COLLECTION).where(
            "retain_until", "<=", now.isoformat()
        ).stream()
        deleted = 0
        for candidate in candidates:
            digest = getattr(candidate, "id", None)
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError("invalid Firestore snapshot manifest identity")
            manifest_ref = self._manifest_ref(digest)
            transaction = self._client.transaction(max_attempts=self._max_attempts)

            def delete_expired(tx):
                current = next(iter(tx.get(manifest_ref)))
                if not current.exists:
                    return False
                manifest = _manifest(current, digest)
                if manifest["expiry"] > now:
                    return False
                for index in range(manifest["chunk_count"]):
                    tx.delete(self._chunk_ref(digest, index))
                tx.delete(manifest_ref)
                return True

            if firestore.transactional(delete_expired)(transaction):
                if self._read_cache is not None:
                    self._read_cache.discard(digest)
                deleted += 1
        return deleted


class _SnapshotReadCache:
    """One verified blob per warm process, bounded to the Firestore blob limit."""

    def __init__(self, *, max_bytes=_MAX_BLOB_BYTES):
        if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_BLOB_BYTES:
            raise ValueError("snapshot read cache must stay within the blob size limit")
        self._max_bytes = max_bytes
        self._lock = Lock()
        self._entry = None

    def get(self, digest, *, raw_bytes, item_count):
        with self._lock:
            entry = self._entry
            if (entry is None or entry[0] != digest or entry[1] != raw_bytes
                    or entry[2] != item_count):
                return None
            return entry[3]

    def put(self, digest, *, raw_bytes, item_count, raw):
        if not isinstance(raw, bytes) or len(raw) > self._max_bytes:
            return
        with self._lock:
            self._entry = (digest, raw_bytes, item_count, raw)

    def discard(self, digest):
        with self._lock:
            if self._entry is not None and self._entry[0] == digest:
                self._entry = None

    def clear(self):
        with self._lock:
            self._entry = None


_WARM_COLLECTION_SNAPSHOT_READ_CACHE = _SnapshotReadCache()
