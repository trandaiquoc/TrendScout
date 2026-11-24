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
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from app.storage.local_file_security import secure_private_directory

_STATE_KEY = "collections-live-snapshot-v1"
_RETENTION = timedelta(days=3650)
# Leave room for base64 and the JSON envelope inside the 5.7 MB chunked-blob
# budget. The encoded chunks remain below Firestore's 10 MiB transaction limit.
_MAX_COMPRESSED_BYTES = 4_200_000
_MAX_DATABASE_BYTES = 80_000_000
# Each segment is independently bounded by the existing blob transaction limit.
# The pointer is committed only after all segments are durable.
_MAX_SNAPSHOT_SEGMENTS = 20
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
_LATEST_DATASET_SUMMARY_VERSION = 1
_MAX_LATEST_DATASET_SUMMARY_BYTES = 48_000
_logger = logging.getLogger(__name__)
_NO_PRELOADED_POINTER = object()
_RESTORED_IMAGE_CACHE_SECONDS = 60
_restored_image_cache_lock = Lock()
_restored_image_cache = None


def _cached_restored_image(key):
    global _restored_image_cache
    with _restored_image_cache_lock:
        entry = _restored_image_cache
        if entry is not None:
            if time.monotonic() >= entry[1]:
                _restored_image_cache = None
            elif entry[0] == key:
                return entry[2]
    return None


def _remember_restored_image(key, raw):
    global _restored_image_cache
    with _restored_image_cache_lock:
        _restored_image_cache = (key, time.monotonic() + _RESTORED_IMAGE_CACHE_SECONDS, raw)


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
        self._latest_dataset_summary_provider = None
        self._latest_dataset_summary_present = False
        self._preloaded_pointer = _NO_PRELOADED_POINTER
        self._loaded = False

    def set_latest_dataset_summary_provider(self, provider) -> None:
        """Attach the read-only query used to index the saved dashboard summary."""
        if not callable(provider):
            raise TypeError("latest dataset summary provider must be callable")
        self._latest_dataset_summary_provider = provider

    @staticmethod
    def _valid_pointer(state) -> bool:
        return (
            isinstance(state, dict)
            and type(state.get("schema")) is int
            and state.get("schema") == 1
            and type(state.get("version")) is int
            and state["version"] >= 1
            and (isinstance(state.get("snapshot_ref"), dict)
                 if "snapshot_refs" not in state else
                 isinstance(state["snapshot_refs"], list)
                 and 1 <= len(state["snapshot_refs"]) <= _MAX_SNAPSHOT_SEGMENTS
                 and all(isinstance(ref, dict) for ref in state["snapshot_refs"]))
            and isinstance(state.get("sqlite_sha256"), str)
            and _SHA256.fullmatch(state["sqlite_sha256"]) is not None
        )

    @staticmethod
    def _summary_from_pointer(state):
        if (not isinstance(state, dict)
                or state.get("latest_dataset_summary_version")
                != _LATEST_DATASET_SUMMARY_VERSION
                or "latest_dataset_summary" not in state):
            return False, None
        summary = state["latest_dataset_summary"]
        if summary is not None and not isinstance(summary, dict):
            return False, None
        try:
            encoded = json.dumps(
                summary, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            return False, None
        if len(encoded) > _MAX_LATEST_DATASET_SUMMARY_BYTES:
            return False, None
        return True, json.loads(encoded)

    def read_latest_dataset_summary(self):
        """Read the atomic dashboard summary without downloading the SQLite image.

        ``(False, None)`` means an older pointer needs the regular snapshot path.
        An empty database is represented by ``(True, None)``.
        """
        read_many = getattr(self._state, "read_many", None)
        state = (read_many([_STATE_KEY])[_STATE_KEY] if callable(read_many) else
                 self._state.transact(_STATE_KEY, lambda current: (current, current)))
        self._preloaded_pointer = state
        if not state:
            self.version = 0
            self._latest_dataset_summary_present = True
            return True, None
        if not self._valid_pointer(state):
            raise CollectionSnapshotUnavailable(
                "shared Collections pointer is invalid",
                failure_reason="INVALID_SHARED_POINTER",
            )
        available, summary = self._summary_from_pointer(state)
        self.version = state["version"]
        self._sha256 = state["sqlite_sha256"]
        self._latest_dataset_summary_present = available
        return available, summary

    def load(self) -> int:
        """Restore the last committed image; leave a missing ledger as an empty DB."""
        if self._preloaded_pointer is not _NO_PRELOADED_POINTER:
            state = self._preloaded_pointer
            self._preloaded_pointer = _NO_PRELOADED_POINTER
        else:
            read_many = getattr(self._state, "read_many", None)
            if callable(read_many):
                state = read_many([_STATE_KEY])[_STATE_KEY]
            else:
                state = self._state.transact(_STATE_KEY, lambda current: (current, current))
        if not state:
            self.version = 0
            self._sha256 = None
            self._content_sha256 = None
            self._latest_dataset_summary_present = False
            self._loaded = True
            return self.version
        if not self._valid_pointer(state):
            raise CollectionSnapshotUnavailable(
                "shared Collections pointer is invalid",
                failure_reason="INVALID_SHARED_POINTER",
            )
        self._latest_dataset_summary_present = self._summary_from_pointer(state)[0]
        try:
            references = state.get("snapshot_refs", [state.get("snapshot_ref")])
            payloads = []
            def read_segment(reference):
                return self._blobs.get(reference, now=_utc(self._utcnow()))

            if len(references) > 1:
                with ThreadPoolExecutor(max_workers=min(4, len(references))) as executor:
                    results = list(executor.map(read_segment, references))
            else:
                results = [read_segment(references[0])]
            for records in results:
                if (not isinstance(records, list) or len(records) != 1
                        or not isinstance(records[0], dict)
                        or records[0].get("encoding") != "sqlite-gzip-base64-v1"
                        or not isinstance(records[0].get("payload"), str)):
                    raise ValueError("invalid Collections snapshot payload")
                payloads.append(records[0]["payload"])
            key = (state["sqlite_sha256"], tuple(
                (reference.get("digest"), reference.get("raw_bytes"), reference.get("item_count"))
                for reference in references
            ))
            raw = _cached_restored_image(key)
            if raw is None:
                pieces = []
                for payload in payloads:
                    piece = base64.b64decode(payload, validate=True)
                    if len(piece) > _MAX_COMPRESSED_BYTES:
                        raise ValueError("Collections snapshot segment exceeds its size limit")
                    pieces.append(piece)
                raw = self._decompress_bounded(b"".join(pieces))
                if hashlib.sha256(raw).hexdigest() != state["sqlite_sha256"]:
                    raise ValueError("Collections snapshot checksum mismatch")
                _remember_restored_image(key, raw)
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
        started = time.perf_counter()
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
            backup_done = time.perf_counter()
            content_digest = self._content_digest(backup_path)
            digest_done = time.perf_counter()
            if content_digest == self._content_sha256:
                summary_pointer = self._latest_dataset_summary_metadata()
                if summary_pointer and not self._latest_dataset_summary_present:
                    self._backfill_latest_dataset_summary(summary_pointer)
                _logger.info(
                    "Collections snapshot timing unchanged=true database_bytes=%d backup_ms=%d digest_ms=%d total_ms=%d",
                    len(raw), int((backup_done-started)*1000),
                    int((digest_done-backup_done)*1000), int((time.perf_counter()-started)*1000),
                )
                return self.version
            compressed = gzip.compress(raw, mtime=0)
            compression_done = time.perf_counter()
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
        if len(compressed) > _MAX_COMPRESSED_BYTES * _MAX_SNAPSHOT_SEGMENTS:
            raise CollectionSnapshotUnavailable(
                "Collections snapshot exceeds its storage limit",
                failure_reason="COMPRESSED_SNAPSHOT_TOO_LARGE",
                database_size_bytes=len(raw),
                compressed_size_bytes=len(compressed),
            )

        now = _utc(self._utcnow())
        digest = hashlib.sha256(raw).hexdigest()
        summary_pointer = self._latest_dataset_summary_metadata()
        upload_started = time.perf_counter()
        try:
            offsets = range(0, len(compressed), _MAX_COMPRESSED_BYTES)

            def upload_segment(offset):
                segment = compressed[offset:offset + _MAX_COMPRESSED_BYTES]
                return self._blobs.put(
                    [{"encoding": "sqlite-gzip-base64-v1",
                      "payload": base64.b64encode(segment).decode("ascii")}],
                    now=now,
                    retain_until=now + _RETENTION,
                    binary_chunks=True,
                )

            if len(offsets) > 1:
                # Blobs are immutable and independently committed. The pointer
                # below remains the sole atomic publication boundary.
                with ThreadPoolExecutor(max_workers=min(4, len(offsets))) as executor:
                    references = list(executor.map(upload_segment, offsets))
            else:
                references = [upload_segment(0)]
            upload_done = time.perf_counter()
        except Exception as error:
            raise CollectionSnapshotUnavailable(
                "Collections snapshot could not be uploaded",
                failure_reason="BLOB_UPLOAD_FAILED", cause_class=_exception_class(error),
            ) from None
        next_version = self.version + 1
        snapshot_pointer = ({"snapshot_ref": references[0]} if len(references) == 1
                            else {"snapshot_refs": references})
        updated = {"schema": 1, "version": next_version,
                   **snapshot_pointer, "sqlite_sha256": digest,
                   "sqlite_content_sha256": content_digest,
                   "updated_at": now.isoformat(), **summary_pointer}

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
        self._latest_dataset_summary_present = bool(summary_pointer)
        finished = time.perf_counter()
        _logger.info(
            "Collections snapshot timing unchanged=false database_bytes=%d compressed_bytes=%d segments=%d "
            "backup_ms=%d digest_ms=%d compression_ms=%d preparation_ms=%d upload_ms=%d commit_ms=%d total_ms=%d",
            len(raw), len(compressed), len(references), int((backup_done-started)*1000),
            int((digest_done-backup_done)*1000), int((compression_done-digest_done)*1000),
            int((upload_started-compression_done)*1000), int((upload_done-upload_started)*1000),
            int((finished-upload_done)*1000), int((finished-started)*1000),
        )
        return self.version

    def _backfill_latest_dataset_summary(self, summary_pointer: dict) -> None:
        """Add optional metadata to the current pointer without replacing its blob."""
        if (type(self.version) is not int or self.version < 1
                or not isinstance(self._sha256, str)
                or _SHA256.fullmatch(self._sha256) is None or not summary_pointer):
            return

        def operation(current):
            if (not self._valid_pointer(current)
                    or current["version"] != self.version
                    or current["sqlite_sha256"] != self._sha256):
                raise CollectionSnapshotConflict("Collections changed in another request")
            available, _summary = self._summary_from_pointer(current)
            updated = current if available else {**current, **summary_pointer}
            return updated, updated

        try:
            current = self._state.transact(_STATE_KEY, operation)
        except CollectionSnapshotConflict:
            raise
        except Exception as error:
            _logger.warning(
                "Firestore Collections latest dataset summary backfill skipped error_type=%s",
                _exception_class(error),
            )
            return
        self._latest_dataset_summary_present = self._summary_from_pointer(current)[0]

    def _latest_dataset_summary_metadata(self) -> dict:
        provider = self._latest_dataset_summary_provider
        if provider is None:
            return {}
        try:
            summary = provider()
            if summary is not None and not isinstance(summary, dict):
                raise ValueError("latest dataset summary must be a mapping or null")
            encoded = json.dumps(
                summary, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded) > _MAX_LATEST_DATASET_SUMMARY_BYTES:
                raise ValueError("latest dataset summary exceeds its bounded size")
        except Exception as error:
            _logger.warning(
                "Firestore Collections latest dataset summary omitted error_type=%s",
                _exception_class(error),
            )
            return {}
        return {
            "latest_dataset_summary_version": _LATEST_DATASET_SUMMARY_VERSION,
            "latest_dataset_summary": json.loads(encoded),
        }
