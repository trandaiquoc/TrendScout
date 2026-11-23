"""Request-scoped SQLite working copy persisted through the shared Firestore DB.

SQLite remains the query engine used by Collections. On Vercel, the complete
database image is compressed and stored as a bounded Firestore snapshot; a
versioned pointer prevents concurrent requests from silently overwriting each
other. This module never creates credentials or a Firestore client.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage.local_file_security import secure_private_directory

_STATE_KEY = "collections-live-snapshot-v1"
_RETENTION = timedelta(days=3650)
# Leave room for base64 and the JSON envelope inside the 5.7 MB chunked-blob
# budget. The encoded chunks remain below Firestore's 10 MiB transaction limit.
_MAX_COMPRESSED_BYTES = 4_200_000
_MAX_DATABASE_BYTES = 80_000_000
_SAFE_EXCEPTION_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SNAPSHOT_FAILURE_REASONS = frozenset({
    "INVALID_SHARED_POINTER",
    "RESTORE_FAILED",
    "WORKING_DATABASE_UNAVAILABLE",
    "SNAPSHOT_PATH_EXISTS",
    "DATABASE_TOO_LARGE",
    "DATABASE_SNAPSHOT_FAILED",
    "COMPRESSED_SNAPSHOT_TOO_LARGE",
    "BLOB_UPLOAD_FAILED",
    "SAVE_CONFIRMATION_FAILED",
    "SNAPSHOT_COMMIT_FAILED",
})


class CollectionSnapshotConflict(RuntimeError):
    """Another Vercel request committed a newer Collections snapshot."""


class CollectionSnapshotUnavailable(RuntimeError):
    """The shared Collections snapshot is missing, invalid, or cannot be saved."""

    def __init__(self, message: str, *, failure_reason: str,
                 cause_class: str | None = None,
                 confirmation_exception_class: str | None = None,
                 database_size_bytes: int | None = None,
                 compressed_size_bytes: int | None = None):
        if failure_reason not in _SNAPSHOT_FAILURE_REASONS:
            raise ValueError("invalid Collections snapshot failure reason")
        super().__init__(message)
        self.failure_reason = failure_reason
        self.cause_class = self._safe_exception_class(cause_class)
        self.confirmation_exception_class = self._safe_exception_class(
            confirmation_exception_class
        )
        self.database_size_bytes = self._safe_size(database_size_bytes)
        self.compressed_size_bytes = self._safe_size(compressed_size_bytes)

    @staticmethod
    def _safe_exception_class(value):
        if value is None:
            return None
        return value if isinstance(value, str) and _SAFE_EXCEPTION_CLASS.fullmatch(value) else "UnknownError"

    @staticmethod
    def _safe_size(value):
        return value if type(value) is int and value >= 0 else None


def _exception_class(error: Exception) -> str:
    value = type(error).__name__
    return value if _SAFE_EXCEPTION_CLASS.fullmatch(value) else "UnknownError"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware collection snapshot clock required")
    return value.astimezone(timezone.utc)


class FirestoreCollectionSnapshot:
    """Restore and conditionally publish one Vercel Collections SQLite image."""

    def __init__(self, client, path: Path, *, utcnow=lambda: datetime.now(timezone.utc)):
        if (not callable(getattr(client, "collection", None))
                or not callable(getattr(client, "transaction", None))
                or not callable(utcnow)):
            raise TypeError("injected Firestore client and clock required")
        self.path = Path(path)
        secure_private_directory(self.path.parent)
        self._utcnow = utcnow
        from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
        from app.storage.firestore_snapshot_blobs import (
            _WARM_COLLECTION_SNAPSHOT_READ_CACHE,
            FirestoreSnapshotBlobs,
        )

        self._state = FirestoreAtomicStateStore(client)
        self._blobs = FirestoreSnapshotBlobs(
            client, read_cache=_WARM_COLLECTION_SNAPSHOT_READ_CACHE
        )
        self.version = 0
        self._sha256 = None
        self._content_sha256 = None
        self._loaded = False

    def load(self) -> int:
        """Restore the last committed image; leave a missing ledger as an empty DB."""
        read_many = getattr(self._state, "read_many", None)
        if callable(read_many):
            state = read_many([_STATE_KEY])[_STATE_KEY]
        else:
            state = self._state.transact(_STATE_KEY, lambda current: (current, current))
        if not state:
            self.version = 0
            self._sha256 = None
            self._content_sha256 = None
            self._loaded = True
            return self.version
        if (not isinstance(state, dict) or type(state.get("schema")) is not int
                or state.get("schema") != 1 or type(state.get("version")) is not int
                or state["version"] < 1 or not isinstance(state.get("snapshot_ref"), dict)
                or not isinstance(state.get("sqlite_sha256"), str)
                or not _SHA256.fullmatch(state["sqlite_sha256"])):
            raise CollectionSnapshotUnavailable(
                "shared Collections pointer is invalid",
                failure_reason="INVALID_SHARED_POINTER",
            )
        try:
            records = self._blobs.get(state["snapshot_ref"], now=_utc(self._utcnow()))
            if (not isinstance(records, list) or len(records) != 1
                    or not isinstance(records[0], dict)
                    or records[0].get("encoding") != "sqlite-gzip-base64-v1"
                    or not isinstance(records[0].get("payload"), str)):
                raise ValueError("invalid Collections snapshot payload")
            compressed = base64.b64decode(records[0]["payload"], validate=True)
            if len(compressed) > _MAX_COMPRESSED_BYTES:
                raise ValueError("Collections snapshot exceeds its compressed size limit")
            raw = self._decompress_bounded(compressed)
            if hashlib.sha256(raw).hexdigest() != state["sqlite_sha256"]:
                raise ValueError("Collections snapshot checksum mismatch")
            if self.path.exists() or self.path.is_symlink():
                raise ValueError("Collections working path already exists")
            with self.path.open("xb") as target:
                target.write(raw)
            self.path.chmod(0o600)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, gzip.BadGzipFile) as error:
            raise CollectionSnapshotUnavailable(
                "shared Collections snapshot could not be restored",
                failure_reason="RESTORE_FAILED", cause_class=_exception_class(error),
            ) from None
        self.version = state["version"]
        self._sha256 = state["sqlite_sha256"]
        content_digest = state.get("sqlite_content_sha256")
        self._content_sha256 = (
            content_digest if isinstance(content_digest, str)
            and _SHA256.fullmatch(content_digest)
            else self._content_digest(self.path)
        )
        self._loaded = True
        return self.version

    @staticmethod
    def _decompress_bounded(compressed: bytes) -> bytes:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as archive:
            chunks = []
            size = 0
            while True:
                chunk = archive.read(min(1024 * 1024, _MAX_DATABASE_BYTES - size + 1))
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > _MAX_DATABASE_BYTES:
                    raise ValueError("Collections snapshot exceeds its expanded size limit")
                chunks.append(chunk)

    @staticmethod
    def _content_digest(path: Path) -> str:
        """Hash stable SQL schema/data rather than SQLite's changing file header."""
        database = sqlite3.connect(str(path), timeout=10)
        try:
            database.execute("PRAGMA query_only=ON")
            digest = hashlib.sha256()
            for statement in database.iterdump():
                digest.update(statement.encode("utf-8"))
                digest.update(b"\n")
            return digest.hexdigest()
        finally:
            database.close()

    @staticmethod
    def _compact_backup(path: Path, *, discard_embedding_cache: bool = False) -> None:
        """Compact a temporary snapshot, optionally dropping recomputable vectors."""
        database = sqlite3.connect(str(path), timeout=10)
        try:
            if discard_embedding_cache:
                cache_table = database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cache'"
                ).fetchone()
                if cache_table:
                    database.execute("DELETE FROM cache")
                    database.commit()
            database.execute("VACUUM")
        finally:
            database.close()

    def save(self) -> int:
        """Publish a compressed SQLite image only if its base version is current."""
        if not self._loaded or self.path.is_symlink() or not self.path.is_file():
            raise CollectionSnapshotUnavailable(
                "Collections working database is unavailable",
                failure_reason="WORKING_DATABASE_UNAVAILABLE",
            )
        backup_path = self.path.with_name(self.path.name + ".snapshot")
        if backup_path.exists() or backup_path.is_symlink():
            raise CollectionSnapshotUnavailable(
                "Collections snapshot path already exists",
                failure_reason="SNAPSHOT_PATH_EXISTS",
            )
        try:
            source = sqlite3.connect(str(self.path), timeout=10)
            target = sqlite3.connect(str(backup_path), timeout=10)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            raw = backup_path.read_bytes()
            if len(raw) > _MAX_DATABASE_BYTES:
                self._compact_backup(backup_path)
                raw = backup_path.read_bytes()
            if len(raw) > _MAX_DATABASE_BYTES:
                # Embedding vectors are a performance cache and can be rebuilt.
                # Prune them only from this temporary copy when required to keep
                # the shared, bounded snapshot within its storage contract.
                self._compact_backup(backup_path, discard_embedding_cache=True)
                raw = backup_path.read_bytes()
            if len(raw) > _MAX_DATABASE_BYTES:
                raise CollectionSnapshotUnavailable(
                    "Collections database exceeds its storage limit",
                    failure_reason="DATABASE_TOO_LARGE",
                    database_size_bytes=len(raw),
                )
            content_digest = self._content_digest(backup_path)
            compressed = gzip.compress(raw, mtime=0)
            if len(compressed) > _MAX_COMPRESSED_BYTES:
                self._compact_backup(backup_path, discard_embedding_cache=True)
                raw = backup_path.read_bytes()
                if len(raw) > _MAX_DATABASE_BYTES:
                    raise CollectionSnapshotUnavailable(
                        "Collections database exceeds its storage limit",
                        failure_reason="DATABASE_TOO_LARGE",
                        database_size_bytes=len(raw),
                    )
                content_digest = self._content_digest(backup_path)
                compressed = gzip.compress(raw, mtime=0)
        except (OSError, sqlite3.Error) as error:
            raise CollectionSnapshotUnavailable(
                "Collections database could not be snapshotted",
                failure_reason="DATABASE_SNAPSHOT_FAILED", cause_class=_exception_class(error),
            ) from None
        finally:
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
        if len(compressed) > _MAX_COMPRESSED_BYTES:
            raise CollectionSnapshotUnavailable(
                "Collections snapshot exceeds its storage limit",
                failure_reason="COMPRESSED_SNAPSHOT_TOO_LARGE",
                database_size_bytes=len(raw),
                compressed_size_bytes=len(compressed),
            )

        now = _utc(self._utcnow())
        digest = hashlib.sha256(raw).hexdigest()
        if content_digest == self._content_sha256:
            return self.version

        try:
            reference = self._blobs.put(
                [{"encoding": "sqlite-gzip-base64-v1",
                  "payload": base64.b64encode(compressed).decode("ascii")}],
                now=now,
                retain_until=now + _RETENTION,
            )
        except Exception as error:
            raise CollectionSnapshotUnavailable(
                "Collections snapshot could not be uploaded",
                failure_reason="BLOB_UPLOAD_FAILED", cause_class=_exception_class(error),
            ) from None
        next_version = self.version + 1
        updated = {"schema": 1, "version": next_version,
                   "snapshot_ref": reference, "sqlite_sha256": digest,
                   "sqlite_content_sha256": content_digest,
                   "updated_at": now.isoformat()}

        def operation(current):
            current_version = current.get("version", 0) if isinstance(current, dict) else -1
            if current_version != self.version:
                raise CollectionSnapshotConflict("Collections changed in another request")
            return updated, updated

        try:
            self._state.transact(_STATE_KEY, operation)
        except CollectionSnapshotConflict:
            raise
        except Exception as commit_error:
            # A network failure can leave the transaction outcome uncertain. Read
            # the pointer once and accept only this exact committed image.
            try:
                current = self._state.transact(_STATE_KEY, lambda value: (value, value))
            except Exception as confirmation_error:
                raise CollectionSnapshotUnavailable(
                    "Collections save confirmation failed",
                    failure_reason="SAVE_CONFIRMATION_FAILED",
                    cause_class=_exception_class(commit_error),
                    confirmation_exception_class=_exception_class(confirmation_error),
                ) from None
            if not isinstance(current, dict) or current.get("version") != next_version \
                    or current.get("sqlite_sha256") != digest:
                raise CollectionSnapshotUnavailable(
                    "Collections snapshot could not be committed",
                    failure_reason="SNAPSHOT_COMMIT_FAILED",
                    cause_class=_exception_class(commit_error),
                ) from None
        self.version = next_version
        self._sha256 = digest
        self._content_sha256 = content_digest
        return self.version
