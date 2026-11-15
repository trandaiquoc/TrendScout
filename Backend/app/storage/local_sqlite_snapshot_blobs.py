"""Bounded, expiring SQLite blobs used by the normal local LIVE checkpoint journal."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from contextlib import closing
from datetime import datetime, timezone

from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore

_MAX_BLOB_BYTES = 4_000_000
_MAX_STORED_BYTES = 128 * 1024 * 1024
_MAX_BLOBS = 256


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware snapshot timestamp required")
    return value.astimezone(timezone.utc)


class LocalSQLiteSnapshotBlobs:
    """Store bounded JSON lists outside the small atomic admission-state document."""

    def __init__(self, store: LocalSQLiteAtomicStateStore):
        if type(store) is not LocalSQLiteAtomicStateStore or store.data_mode != "LIVE":
            raise ValueError("owner-only local LIVE SQLite storage is required")
        self.store = store
        with closing(self.store._connect()) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS source_snapshot_blobs (
                digest TEXT PRIMARY KEY,
                payload BLOB NOT NULL,
                raw_bytes INTEGER NOT NULL,
                item_count INTEGER NOT NULL,
                retain_until TEXT NOT NULL
            )""")

    def purge_expired(self, *, now):
        now = _utc(now).isoformat()
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM source_snapshot_blobs WHERE retain_until<=?", (now,)
            )
            connection.commit()
            return cursor.rowcount

    def put(self, records, *, now, retain_until):
        now, retain_until = _utc(now), _utc(retain_until)
        if not isinstance(records, list) or len(records) > 2000 or retain_until <= now:
            raise ValueError("bounded records and a future retention deadline are required")
        try:
            raw = json.dumps(records, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("snapshot records must be finite UTF-8 JSON") from None
        if len(raw) > _MAX_BLOB_BYTES:
            raise ValueError("snapshot record blob exceeds its bounded storage budget")
        digest = hashlib.sha256(raw).hexdigest()
        compressed = zlib.compress(raw, level=3)

        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM source_snapshot_blobs WHERE retain_until<=?", (now.isoformat(),)
                )
                existing = connection.execute(
                    "SELECT payload,raw_bytes,item_count,retain_until FROM source_snapshot_blobs "
                    "WHERE digest=?", (digest,)
                ).fetchone()
                if existing:
                    if (existing[1] != len(raw) or existing[2] != len(records)
                            or self._decode(existing[0], digest, existing[1]) != raw):
                        raise ValueError("snapshot blob digest collision or invalid stored data")
                    expiry = max(_utc(datetime.fromisoformat(existing[3])), retain_until)
                    connection.execute(
                        "UPDATE source_snapshot_blobs SET retain_until=? WHERE digest=?",
                        (expiry.isoformat(), digest),
                    )
                else:
                    count, stored_bytes = connection.execute(
                        "SELECT count(*),COALESCE(sum(length(payload)),0) "
                        "FROM source_snapshot_blobs"
                    ).fetchone()
                    if count >= _MAX_BLOBS or stored_bytes + len(compressed) > _MAX_STORED_BYTES:
                        raise ValueError("snapshot blob staging capacity is full")
                    connection.execute(
                        "INSERT INTO source_snapshot_blobs "
                        "(digest,payload,raw_bytes,item_count,retain_until) VALUES(?,?,?,?,?)",
                        (digest, compressed, len(raw), len(records), retain_until.isoformat()),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {"digest": digest, "raw_bytes": len(raw), "item_count": len(records)}

    def get(self, reference, *, now):
        now = _utc(now)
        if (not isinstance(reference, dict)
                or not isinstance(reference.get("digest"), str)
                or len(reference["digest"]) != 64
                or type(reference.get("raw_bytes")) is not int
                or not 0 <= reference["raw_bytes"] <= _MAX_BLOB_BYTES
                or type(reference.get("item_count")) is not int
                or not 0 <= reference["item_count"] <= 2000):
            raise ValueError("invalid snapshot blob reference")
        connection = sqlite3.connect(
            self.store.path, timeout=self.store.timeout_seconds, isolation_level=None
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT payload,raw_bytes,item_count,retain_until "
                "FROM source_snapshot_blobs WHERE digest=?", (reference["digest"],)
            ).fetchone()
        finally:
            connection.close()
        if not row or _utc(datetime.fromisoformat(row[3])) <= now:
            raise ValueError("snapshot blob is missing or expired")
        if row[1] != reference["raw_bytes"] or row[2] != reference["item_count"]:
            raise ValueError("snapshot blob reference does not match stored metadata")
        raw = self._decode(row[0], reference["digest"], row[1])
        try:
            records = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("snapshot blob JSON is invalid") from None
        if not isinstance(records, list) or len(records) != row[2]:
            raise ValueError("snapshot blob record count is invalid")
        return records

    @staticmethod
    def _decode(payload, digest, raw_bytes):
        if not isinstance(payload, bytes) or type(raw_bytes) is not int or not 0 <= raw_bytes <= _MAX_BLOB_BYTES:
            raise ValueError("invalid stored snapshot blob")
        try:
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(payload, _MAX_BLOB_BYTES + 1)
            if (len(raw) > _MAX_BLOB_BYTES or decompressor.unconsumed_tail
                    or decompressor.unused_data or not decompressor.eof):
                raise ValueError("stored snapshot blob exceeds its bounded size")
            raw += decompressor.flush()
            if len(raw) > _MAX_BLOB_BYTES:
                raise ValueError("stored snapshot blob exceeds its bounded size")
        except zlib.error:
            raise ValueError("stored snapshot blob is corrupt") from None
        if len(raw) != raw_bytes or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("stored snapshot blob integrity check failed")
        return raw
