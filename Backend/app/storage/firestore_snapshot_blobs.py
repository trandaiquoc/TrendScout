"""Bounded, content-addressed JSON snapshots split across Firestore documents.

New content is published with its manifest and chunks in one transaction.
Active immutable content is reused without rewriting bytes; retention updates
patch the TTL field on its manifest and chunks atomically.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock

from google.cloud import firestore

_COLLECTION = "_ts_d5_cache_blobs"
_MAX_BLOB_BYTES = 5_700_000
_MAX_ITEMS = 5000
_CHUNK_BYTES = 196_608
_MAX_BATCH_RAW_BYTES = 7_000_000
_MAX_BATCH_WRITES = 450
_READ_CACHE_TTL = timedelta(seconds=60)
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
            or value.get("chunk_encoding", "base64") not in ("base64", "bytes")
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

    def put(self, records, *, now: datetime, retain_until: datetime,
            binary_chunks: bool = False) -> dict:
        return self.put_many(
            [records], now=now, retain_until=retain_until,
            binary_chunks=binary_chunks,
        )[0]

    def put_many(self, record_sets, *, now: datetime, retain_until: datetime,
                 binary_chunks: bool = False) -> list[dict]:
        """Publish bounded blobs in a small number of Firestore transactions.

        Each blob remains content-addressed and independently readable. A
        failed later batch leaves only unreferenced immutable content; callers
        publish their pointer after every batch has completed.
        """
        now, retain_until = _utc(now), _utc(retain_until)
        if (type(binary_chunks) is not bool or not isinstance(record_sets, list)
                or not record_sets or len(record_sets) > 256 or retain_until <= now):
            raise ValueError("bounded records and a future retention deadline are required")
        prepared_by_digest = {}
        output_digests = []
        for records in record_sets:
            if not isinstance(records, list) or len(records) > _MAX_ITEMS:
                raise ValueError("bounded records and a future retention deadline are required")
            try:
                raw = json.dumps(records, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise ValueError("snapshot records must be finite UTF-8 JSON") from None
            if len(raw) > _MAX_BLOB_BYTES:
                raise ValueError("snapshot record blob exceeds its bounded storage budget")
            digest = hashlib.sha256(raw).hexdigest()
            output_digests.append(digest)
            if digest not in prepared_by_digest:
                chunks = [raw[offset:offset + _CHUNK_BYTES]
                          for offset in range(0, len(raw), _CHUNK_BYTES)] or [b""]
                prepared_by_digest[digest] = {
                    "digest": digest,
                    "raw": raw,
                    "item_count": len(records),
                    "chunks": chunks,
                }

        # Keep transactions below Firestore's 10 MiB API request size and this
        # conservative operation-count ceiling, including document overhead.
        batches = []
        batch = []
        batch_bytes = 0
        batch_writes = 0
        for item in prepared_by_digest.values():
            item_writes = len(item["chunks"]) + 1
            if batch and (batch_bytes + len(item["raw"]) > _MAX_BATCH_RAW_BYTES
                          or batch_writes + item_writes > _MAX_BATCH_WRITES):
                batches.append(batch)
                batch, batch_bytes, batch_writes = [], 0, 0
            batch.append(item)
            batch_bytes += len(item["raw"])
            batch_writes += item_writes
        if batch:
            batches.append(batch)

        def write_batch(items):
            references_by_digest = {item["digest"]: self._manifest_ref(item["digest"])
                                    for item in items}
            chunk_references = {
                item["digest"]: [
                    self._chunk_ref(item["digest"], index)
                    for index in range(len(item["chunks"]))
                ]
                for item in items
            }
            transaction = self._client.transaction(max_attempts=self._max_attempts)

            def write_batch(tx):
                # Firestore transactions require all reads to precede writes.
                snapshots = {}
                for item in items:
                    snapshots[item["digest"]] = next(iter(tx.get(
                        references_by_digest[item["digest"]]
                    )))

                writes = []
                for item in items:
                    digest = item["digest"]
                    snapshot = snapshots[digest]
                    manifest_ref = references_by_digest[digest]
                    chunks_refs = chunk_references[digest]
                    effective_expiry = retain_until
                    encoding = "bytes" if binary_chunks else "base64"
                    if snapshot.exists:
                        previous = _manifest(snapshot, digest)
                        if (previous["raw_bytes"] != len(item["raw"])
                                or previous["item_count"] != item["item_count"]
                                or previous["chunk_count"] != len(item["chunks"])):
                            raise ValueError("snapshot digest collision or invalid manifest")
                        effective_expiry = max(previous["expiry"], retain_until)
                        encoding = previous.get("chunk_encoding", "base64")
                        if previous["expiry"] > now:
                            if effective_expiry == previous["expiry"]:
                                continue
                            retention_text = effective_expiry.isoformat()
                            writes.extend(("update", reference, {"retain_until": retention_text})
                                          for reference in chunks_refs)
                            writes.append(("update", manifest_ref,
                                           {"retain_until": retention_text}))
                            continue

                    retention_text = effective_expiry.isoformat()
                    for index, (reference, content) in enumerate(
                            zip(chunks_refs, item["chunks"], strict=True)):
                        writes.append(("set", reference, {
                            "schema": 1,
                            "digest": digest,
                            "index": index,
                            **({"data_bytes": content} if encoding == "bytes" else
                               {"data_b64": base64.b64encode(content).decode("ascii")}),
                            "retain_until": retention_text,
                        }))
                    writes.append(("set", manifest_ref, {
                        "schema": 1,
                        "digest": digest,
                        "raw_bytes": len(item["raw"]),
                        "item_count": item["item_count"],
                        "chunk_count": len(item["chunks"]),
                        "retain_until": retention_text,
                        **({"chunk_encoding": "bytes"} if encoding == "bytes" else {}),
                    }))
                if len(writes) > _MAX_BATCH_WRITES:
                    raise ValueError("snapshot transaction exceeds its bounded write count")
                for operation, reference, value in writes:
                    getattr(tx, operation)(reference, value)

            firestore.transactional(write_batch)(transaction)

        if len(batches) == 1:
            write_batch(batches[0])
        else:
            # Immutable blobs have independent manifests. A bounded pool keeps
            # large snapshot uploads from paying one Firestore round trip per
            # batch while the owner pointer remains the publication boundary.
            with ThreadPoolExecutor(max_workers=min(4, len(batches))) as executor:
                list(executor.map(write_batch, batches))

        return [
            {
                "digest": digest,
                "raw_bytes": len(prepared_by_digest[digest]["raw"]),
                "item_count": prepared_by_digest[digest]["item_count"],
            }
            for digest in output_digests
        ]

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
        cached = (self._read_cache.get(
            digest, raw_bytes=reference["raw_bytes"],
            item_count=reference["item_count"], now=now,
        ) if self._read_cache is not None else None)
        cache_hit = cached is not None
        if cached is not None:
            raw = cached["raw"]
            manifest = {
                "raw_bytes": reference["raw_bytes"],
                "item_count": reference["item_count"],
                "chunk_count": cached["chunk_count"],
                "expiry": cached["expiry"],
                "retain_until": cached["expiry"].isoformat(),
            }
        else:
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
            raw = None
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
                        or not isinstance(value.get("retain_until"), str)):
                    raise ValueError("snapshot blob chunk is missing or invalid")
                try:
                    chunk_expiry = _utc(datetime.fromisoformat(value["retain_until"]))
                except (TypeError, ValueError):
                    raise ValueError("snapshot blob chunk retention is invalid") from None
                if chunk_expiry != manifest["expiry"]:
                    raise ValueError("snapshot blob chunk retention does not match its manifest")
                if manifest.get("chunk_encoding", "base64") == "bytes":
                    if type(value.get("data_bytes")) is not bytes or "data_b64" in value:
                        raise ValueError("snapshot blob chunk encoding is invalid")
                    piece = value["data_bytes"]
                else:
                    if not isinstance(value.get("data_b64"), str) or "data_bytes" in value:
                        raise ValueError("snapshot blob chunk encoding is invalid")
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
                    chunk_count=manifest["chunk_count"], expiry=manifest["expiry"],
                    now=now,
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
    """Bounded verified blobs with a short manifest-validation window."""

    def __init__(self, *, max_bytes=2 * _MAX_BLOB_BYTES):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2 * _MAX_BLOB_BYTES:
            raise ValueError("snapshot read cache must stay within the blob size limit")
        self._max_bytes = max_bytes
        self._lock = Lock()
        self._entries = OrderedDict()
        self._size = 0

    def get(self, digest, *, raw_bytes, item_count, now):
        now = _utc(now)
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None or entry[1] != raw_bytes or entry[2] != item_count:
                return None
            if (entry[6] + _READ_CACHE_TTL <= now
                    or entry[5] <= now):
                self._size -= len(entry[3])
                del self._entries[digest]
                return None
            self._entries.move_to_end(digest)
            return {
                "raw": entry[3], "chunk_count": entry[4], "expiry": entry[5],
            }

    def put(self, digest, *, raw_bytes, item_count, raw, chunk_count, expiry, now):
        if not isinstance(raw, bytes) or len(raw) > self._max_bytes:
            return
        now, expiry = _utc(now), _utc(expiry)
        if (type(chunk_count) is not int or chunk_count < 1
                or chunk_count != max(1, (raw_bytes + _CHUNK_BYTES - 1) // _CHUNK_BYTES)
                or expiry <= now):
            return
        with self._lock:
            old = self._entries.pop(digest, None)
            if old is not None:
                self._size -= len(old[3])
            while self._entries and self._size + len(raw) > self._max_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._size -= len(evicted[3])
            self._entries[digest] = (digest, raw_bytes, item_count, raw, chunk_count, expiry, now)
            self._size += len(raw)

    def discard(self, digest):
        with self._lock:
            entry = self._entries.pop(digest, None)
            if entry is not None:
                self._size -= len(entry[3])

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._size = 0


_WARM_COLLECTION_SNAPSHOT_READ_CACHE = _SnapshotReadCache()
