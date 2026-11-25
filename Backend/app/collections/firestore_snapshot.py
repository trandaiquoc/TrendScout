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
# Stable, independently compressed SQLite blocks let Firestore reuse chunks
# whose page bytes did not change.
_RAW_SNAPSHOT_SEGMENT_BYTES = 4_194_304
_MAX_COMPRESSED_SEGMENT_BYTES = 4_200_000
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
_DASHBOARD_BOOTSTRAP_VERSION = 1
_MAX_DASHBOARD_BOOTSTRAP_BYTES = 512_000
_DASHBOARD_BOOTSTRAP_RETENTION = timedelta(days=30)
_RUN_HISTORY_PROJECTION_LIMIT = 20
_logger = logging.getLogger(__name__)
_NO_PRELOADED_POINTER = object()
_RESTORED_IMAGE_CACHE_SECONDS = 60
_restored_image_cache_lock = Lock()
_restored_image_cache = None


def _valid_keyword_page(value):
    return (
        isinstance(value, dict)
        and isinstance(value.get("items"), list)
        and all(isinstance(item, dict) for item in value["items"])
        and type(value.get("total")) is int
        and value["total"] >= 0
        and type(value.get("limit")) is int
        and 1 <= value["limit"] <= 100
        and len(value["items"]) <= value["limit"]
        and type(value.get("offset")) is int
        and value["offset"] >= 0
    )


def _valid_collections_bootstrap(value):
    required = {"items", "suggestions", "mode", "summary"}
    allowed = required | {"keyword_page"}
    return (
        isinstance(value, dict)
        and required <= set(value) <= allowed
        and isinstance(value.get("items"), list)
        and isinstance(value.get("suggestions"), list)
        and isinstance(value.get("summary"), dict)
        and isinstance(value.get("mode"), str)
        and ("keyword_page" not in value or _valid_keyword_page(value["keyword_page"]))
    )


def _valid_algorithm_results_projection(value):
    return (
        isinstance(value, dict)
        and set(value) == {"summary", "items", "limit"}
        and isinstance(value.get("summary"), list)
        and all(isinstance(item, dict) for item in value["summary"])
        and isinstance(value.get("items"), list)
        and all(isinstance(item, dict) for item in value["items"])
        and type(value.get("limit")) is int
        and 1 <= value["limit"] <= 500
    )


def _valid_run_history_projection(value):
    return (
        isinstance(value, dict)
        and set(value) == {"items", "has_more", "limit"}
        and isinstance(value.get("items"), list)
        and len(value["items"]) <= _RUN_HISTORY_PROJECTION_LIMIT
        and all(
            isinstance(item, dict)
            and isinstance(item.get("_cursor_local"), list)
            and len(item["_cursor_local"]) == 2
            and all(isinstance(part, str) for part in item["_cursor_local"])
            for item in value["items"]
        )
        and type(value.get("has_more")) is bool
        and type(value.get("limit")) is int
        and value["limit"] == _RUN_HISTORY_PROJECTION_LIMIT
    )


def _valid_research_bootstrap(value):
    if not isinstance(value, dict) or set(value) != {"keywords", "research"}:
        return False
    keywords = value.get("keywords")
    research = value.get("research")
    return (
        _valid_keyword_page(keywords)
        and keywords.get("limit") == 100
        and isinstance(research, dict)
        and set(research) == {"items", "total", "limit", "offset", "next_offset"}
        and isinstance(research.get("items"), list)
        and len(research["items"]) <= 50
        and all(isinstance(item, dict) for item in research["items"])
        and type(research.get("total")) is int
        and research["total"] >= 0
        and type(research.get("limit")) is int
        and research["limit"] == 50
        and type(research.get("offset")) is int
        and research["offset"] == 0
        and (research.get("next_offset") is None
             or type(research.get("next_offset")) is int
             and research["next_offset"] >= 0)
    )


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


def _snapshot_segment_fingerprint(segment):
    return len(segment), hashlib.sha256(segment).hexdigest()


def _snapshot_record_compressed_bytes(records):
    payload = records[0]["payload"]
    padding = len(payload) - len(payload.rstrip("="))
    return len(payload) * 3 // 4 - padding


def _prepare_snapshot_segment_records(
    raw_segments, *, base_fingerprints=None, base_records=None,
):
    """Reuse restored compressed blocks and encode only changed SQLite pages."""
    fingerprints = tuple(_snapshot_segment_fingerprint(segment) for segment in raw_segments)
    record_sets = [None] * len(raw_segments)
    changed = []
    for index, segment in enumerate(raw_segments):
        if (base_fingerprints is not None and base_records is not None
                and index < len(base_fingerprints) and index < len(base_records)
                and fingerprints[index] == base_fingerprints[index]
                and isinstance(base_records[index], list)
                and len(base_records[index]) == 1
                and isinstance(base_records[index][0], dict)
                and base_records[index][0].get("encoding") == "sqlite-segment-gzip-base64-v1"
                and isinstance(base_records[index][0].get("payload"), str)):
            record_sets[index] = [dict(base_records[index][0])]
        else:
            changed.append((index, segment))

    if len(changed) == 1:
        compressed = [_compress_snapshot_segment(changed[0][1])]
    elif changed:
        with ThreadPoolExecutor(max_workers=min(4, len(changed))) as executor:
            compressed = list(executor.map(
                _compress_snapshot_segment, (segment for _index, segment in changed),
            ))
    else:
        compressed = []

    compressed_bytes = 0
    for payload in record_sets:
        if payload is not None:
            compressed_bytes += _snapshot_record_compressed_bytes(payload)
    for (index, _segment), piece in zip(changed, compressed, strict=True):
        compressed_bytes += len(piece)
        record_sets[index] = [{
            "encoding": "sqlite-segment-gzip-base64-v1",
            "payload": base64.b64encode(piece).decode("ascii"),
        }]
    return record_sets, fingerprints, compressed_bytes


def _compress_snapshot_segment(segment):
    return gzip.compress(segment, compresslevel=6, mtime=0)


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
        self._dashboard_bootstrap_provider = None
        self._dashboard_bootstrap_present = False
        self._dashboard_bootstrap_checked = False
        self._preloaded_pointer = _NO_PRELOADED_POINTER
        self._base_snapshot_segment_fingerprints = None
        self._base_snapshot_record_sets = None
        self._loaded = False

    def set_latest_dataset_summary_provider(self, provider) -> None:
        """Attach the read-only query used to index the saved dashboard summary."""
        if not callable(provider):
            raise TypeError("latest dataset summary provider must be callable")
        self._latest_dataset_summary_provider = provider

    def set_dashboard_bootstrap_provider(self, provider) -> None:
        """Attach a bounded provider for the default dashboard read model."""
        if not callable(provider):
            raise TypeError("dashboard bootstrap provider must be callable")
        self._dashboard_bootstrap_provider = provider

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
            and isinstance(state.get("sqlite_snapshot_encoding", "sqlite-gzip-base64-v1"), str)
            and state.get("sqlite_snapshot_encoding", "sqlite-gzip-base64-v1")
                in {"sqlite-gzip-base64-v1", "sqlite-segment-gzip-base64-v1"}
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

    @staticmethod
    def _dashboard_bootstrap_reference(state):
        if (not isinstance(state, dict)
                or state.get("dashboard_bootstrap_version") != _DASHBOARD_BOOTSTRAP_VERSION
                or state.get("dashboard_snapshot_version") != state.get("version")
                or state.get("dashboard_sqlite_sha256") != state.get("sqlite_sha256")
                or not isinstance(state.get("dashboard_bootstrap_ref"), dict)):
            return None
        return state["dashboard_bootstrap_ref"]

    def read_dashboard_bootstrap(self):
        """Read a version-matched dashboard view without restoring SQLite."""
        state = self._preloaded_pointer
        if state is _NO_PRELOADED_POINTER:
            read_many = getattr(self._state, "read_many", None)
            state = (read_many([_STATE_KEY])[_STATE_KEY] if callable(read_many) else
                     self._state.transact(_STATE_KEY, lambda current: (current, current)))
        self._preloaded_pointer = state
        if not state:
            self.version = 0
            self._latest_dataset_summary_present = True
            self._dashboard_bootstrap_present = False
            self._dashboard_bootstrap_checked = True
            return False, None
        if not self._valid_pointer(state):
            raise CollectionSnapshotUnavailable(
                "shared Collections pointer is invalid",
                failure_reason="INVALID_SHARED_POINTER",
            )
        self.version = state["version"]
        self._sha256 = state["sqlite_sha256"]
        self._latest_dataset_summary_present = self._summary_from_pointer(state)[0]
        reference = self._dashboard_bootstrap_reference(state)
        self._dashboard_bootstrap_present = reference is not None
        self._dashboard_bootstrap_checked = True
        if reference is None:
            return False, None
        try:
            records = self._blobs.get(reference, now=_utc(self._utcnow()))
            if (not isinstance(records, list) or len(records) != 1
                    or not isinstance(records[0], dict)
                    or records[0].get("encoding") != "dashboard-bootstrap-json-v1"
                    or not isinstance(records[0].get("payload"), dict)):
                raise ValueError("invalid dashboard bootstrap blob")
            payload = records[0]["payload"]
            encoded = json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            if (len(encoded) > _MAX_DASHBOARD_BOOTSTRAP_BYTES
                    or not {"item", "analytics", "keywords"} <= set(payload)
                    or not set(payload) <= {
                        "item", "analytics", "keywords", "collections", "algorithm_results",
                        "run_history", "research",
                    }
                    or (payload["item"] is not None and not isinstance(payload["item"], dict))
                    or not isinstance(payload["analytics"], dict)
                    or not isinstance(payload["keywords"], dict)
                    or not isinstance(payload["keywords"].get("items"), list)
                    or "collections" in payload
                    and not _valid_collections_bootstrap(payload["collections"])
                    or "algorithm_results" in payload
                    and not _valid_algorithm_results_projection(payload["algorithm_results"])
                    or "run_history" in payload
                    and not _valid_run_history_projection(payload["run_history"])
                    or "research" in payload
                    and not _valid_research_bootstrap(payload["research"])):
                raise ValueError("invalid dashboard bootstrap payload")
        except Exception as error:
            _logger.warning(
                "Firestore dashboard bootstrap read model ignored error_type=%s",
                _exception_class(error),
            )
            self._dashboard_bootstrap_present = False
            return False, None
        return True, json.loads(encoded)

    def read_run_history_projection(self):
        """Read the bounded local Run History page from the snapshot read model."""
        available, bootstrap = self.read_dashboard_bootstrap()
        if not available or not isinstance(bootstrap, dict):
            return False, False, None
        projection = bootstrap.get("run_history")
        valid = _valid_run_history_projection(projection)
        return True, valid, projection if valid else None

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
            self._dashboard_bootstrap_present = False
            self._base_snapshot_segment_fingerprints = None
            self._base_snapshot_record_sets = None
            self._loaded = True
            return self.version
        if not self._valid_pointer(state):
            raise CollectionSnapshotUnavailable(
                "shared Collections pointer is invalid",
                failure_reason="INVALID_SHARED_POINTER",
            )
        self._latest_dataset_summary_present = self._summary_from_pointer(state)[0]
        if not self._dashboard_bootstrap_checked:
            self._dashboard_bootstrap_present = (
                self._dashboard_bootstrap_reference(state) is not None
            )
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
            snapshot_encoding = state.get(
                "sqlite_snapshot_encoding", "sqlite-gzip-base64-v1"
            )
            payload_encoding = (
                "sqlite-segment-gzip-base64-v1"
                if snapshot_encoding == "sqlite-segment-gzip-base64-v1"
                else "sqlite-gzip-base64-v1"
            )
            for records in results:
                if (not isinstance(records, list) or len(records) != 1
                        or not isinstance(records[0], dict)
                        or records[0].get("encoding") != payload_encoding
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
                for index, payload in enumerate(payloads):
                    piece = base64.b64decode(payload, validate=True)
                    if snapshot_encoding == "sqlite-segment-gzip-base64-v1":
                        if len(piece) > _MAX_COMPRESSED_SEGMENT_BYTES:
                            raise ValueError("Collections segment exceeds its size limit")
                        page = self._decompress_segment_bounded(piece)
                        if (index < len(payloads) - 1
                                and len(page) != _RAW_SNAPSHOT_SEGMENT_BYTES):
                            raise ValueError("Collections snapshot segment is incomplete")
                        pieces.append(page)
                    else:
                        if len(piece) > _MAX_COMPRESSED_BYTES:
                            raise ValueError("Collections snapshot segment exceeds its size limit")
                        pieces.append(piece)
                raw = (b"".join(pieces)
                       if snapshot_encoding == "sqlite-segment-gzip-base64-v1"
                       else self._decompress_bounded(b"".join(pieces)))
                if len(raw) > _MAX_DATABASE_BYTES:
                    raise ValueError("Collections snapshot exceeds its expanded size limit")
                if hashlib.sha256(raw).hexdigest() != state["sqlite_sha256"]:
                    raise ValueError("Collections snapshot checksum mismatch")
                _remember_restored_image(key, raw)
            if snapshot_encoding == "sqlite-segment-gzip-base64-v1":
                expected_segments = max(
                    1, (len(raw) + _RAW_SNAPSHOT_SEGMENT_BYTES - 1)
                    // _RAW_SNAPSHOT_SEGMENT_BYTES,
                )
                if len(references) != expected_segments:
                    raise ValueError("Collections snapshot segment count is invalid")
                raw_segments = [
                    memoryview(raw)[offset:offset + _RAW_SNAPSHOT_SEGMENT_BYTES]
                    for offset in range(0, len(raw), _RAW_SNAPSHOT_SEGMENT_BYTES)
                ] or [memoryview(raw)]
                self._base_snapshot_segment_fingerprints = tuple(
                    _snapshot_segment_fingerprint(segment) for segment in raw_segments
                )
                if len(self._base_snapshot_segment_fingerprints) != len(references):
                    raise ValueError("Collections snapshot segment count is invalid")
                self._base_snapshot_record_sets = tuple(
                    [[dict(records[0])] for records in results]
                )
            else:
                self._base_snapshot_segment_fingerprints = None
                self._base_snapshot_record_sets = None
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

    def _dashboard_bootstrap_records(self, payload=None):
        provider = self._dashboard_bootstrap_provider
        if provider is None and payload is None:
            return None
        try:
            if payload is None:
                payload = provider()
            if isinstance(payload, dict):
                payload = dict(payload)
            if (not isinstance(payload, dict)
                    or not {"item", "analytics", "keywords"} <= set(payload)
                    or not set(payload) <= {
                        "item", "analytics", "keywords", "collections", "algorithm_results",
                        "run_history",
                        "research",
                    }
                    or (payload["item"] is not None and not isinstance(payload["item"], dict))
                    or not isinstance(payload["analytics"], dict)
                    or not isinstance(payload["keywords"], dict)
                    or not isinstance(payload["keywords"].get("items"), list)
                    or "collections" in payload
                    and not _valid_collections_bootstrap(payload["collections"])
                    or "algorithm_results" in payload
                    and not _valid_algorithm_results_projection(payload["algorithm_results"])
                    or "run_history" in payload
                    and not _valid_run_history_projection(payload["run_history"])
                    or "research" in payload
                    and not _valid_research_bootstrap(payload["research"])):
                raise ValueError("dashboard bootstrap provider returned an invalid payload")
            encoded = json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded) > _MAX_DASHBOARD_BOOTSTRAP_BYTES:
                for optional_key in (
                    "algorithm_results", "collections", "run_history", "research",
                ):
                    if optional_key not in payload:
                        continue
                    payload.pop(optional_key)
                    encoded = json.dumps(
                        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ).encode("utf-8")
                    if len(encoded) <= _MAX_DASHBOARD_BOOTSTRAP_BYTES:
                        break
            if len(encoded) > _MAX_DASHBOARD_BOOTSTRAP_BYTES:
                raise ValueError("dashboard bootstrap exceeds its bounded size")
        except Exception as error:
            _logger.warning(
                "Firestore dashboard bootstrap omitted error_type=%s",
                _exception_class(error),
            )
            return None
        return [{"encoding": "dashboard-bootstrap-json-v1",
                 "payload": json.loads(encoded)}]

    @staticmethod
    def _dashboard_bootstrap_pointer(reference, *, snapshot_version, sqlite_sha256):
        return {
            "dashboard_bootstrap_version": _DASHBOARD_BOOTSTRAP_VERSION,
            "dashboard_snapshot_version": snapshot_version,
            "dashboard_sqlite_sha256": sqlite_sha256,
            "dashboard_bootstrap_ref": reference,
        }

    def _dashboard_bootstrap_metadata(self, *, snapshot_version, sqlite_sha256, now,
                                      payload=None):
        records = self._dashboard_bootstrap_records(payload)
        if records is None:
            return {}
        try:
            reference = self._blobs.put(
                records,
                now=now,
                retain_until=now + _DASHBOARD_BOOTSTRAP_RETENTION,
            )
        except Exception as error:
            _logger.warning(
                "Firestore dashboard bootstrap omitted error_type=%s",
                _exception_class(error),
            )
            return {}
        return self._dashboard_bootstrap_pointer(
            reference, snapshot_version=snapshot_version, sqlite_sha256=sqlite_sha256,
        )

    def backfill_dashboard_bootstrap(self, payload, *, replace_existing=False) -> bool:
        """Persist a read model for the exact restored snapshot version."""
        if self._dashboard_bootstrap_present and not replace_existing:
            return True
        if (type(self.version) is not int or self.version < 1
                or not isinstance(self._sha256, str)
                or _SHA256.fullmatch(self._sha256) is None):
            return False
        metadata = self._dashboard_bootstrap_metadata(
            snapshot_version=self.version,
            sqlite_sha256=self._sha256,
            now=_utc(self._utcnow()),
            payload=payload,
        )
        if metadata:
            try:
                self._backfill_optional_metadata(
                    metadata, replace_dashboard_bootstrap=replace_existing,
                )
            except CollectionSnapshotConflict:
                # This is an optional read model. A concurrent writer may
                # advance the SQLite pointer after this request restored it;
                # keep serving the response built from that restored version.
                return False
        return self._dashboard_bootstrap_present

    def _backfill_optional_metadata(
        self, metadata: dict, *, replace_dashboard_bootstrap=False,
    ) -> None:
        """Atomically add read models to their exact existing SQLite version."""
        if (type(self.version) is not int or self.version < 1
                or not isinstance(self._sha256, str)
                or _SHA256.fullmatch(self._sha256) is None
                or not isinstance(metadata, dict) or not metadata):
            return

        def operation(current):
            if (not self._valid_pointer(current)
                    or current["version"] != self.version
                    or current["sqlite_sha256"] != self._sha256):
                raise CollectionSnapshotConflict("Collections changed in another request")
            updated = dict(current)
            if "latest_dataset_summary_version" in metadata:
                available, _summary = self._summary_from_pointer(current)
                if not available:
                    updated.update({
                        key: metadata[key] for key in (
                            "latest_dataset_summary_version", "latest_dataset_summary",
                        ) if key in metadata
                    })
            if "dashboard_bootstrap_ref" in metadata:
                if (replace_dashboard_bootstrap
                        or self._dashboard_bootstrap_reference(current) is None
                        or not self._dashboard_bootstrap_present):
                    updated.update(metadata)
            return updated, updated

        try:
            current = self._state.transact(_STATE_KEY, operation)
        except CollectionSnapshotConflict:
            raise
        except Exception as error:
            _logger.warning(
                "Firestore Collections read model backfill skipped error_type=%s",
                _exception_class(error),
            )
            return
        self._latest_dataset_summary_present = self._summary_from_pointer(current)[0]
        self._dashboard_bootstrap_present = (
            self._dashboard_bootstrap_reference(current) is not None
        )
        self._dashboard_bootstrap_checked = True

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
    def _decompress_segment_bounded(compressed: bytes) -> bytes:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as archive:
            chunks = []
            size = 0
            while True:
                chunk = archive.read(min(
                    1024 * 1024, _RAW_SNAPSHOT_SEGMENT_BYTES - size + 1
                ))
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > _RAW_SNAPSHOT_SEGMENT_BYTES:
                    raise ValueError("Collections snapshot segment exceeds its expanded size limit")
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
                metadata = {}
                now = _utc(self._utcnow())
                if not self._latest_dataset_summary_present:
                    metadata.update(self._latest_dataset_summary_metadata())
                if not self._dashboard_bootstrap_present:
                    metadata.update(self._dashboard_bootstrap_metadata(
                        snapshot_version=self.version,
                        sqlite_sha256=self._sha256,
                        now=now,
                    ))
                if metadata:
                    self._backfill_optional_metadata(metadata)
                _logger.info(
                    "Collections snapshot timing unchanged=true database_bytes=%d backup_ms=%d digest_ms=%d total_ms=%d",
                    len(raw), int((backup_done-started)*1000),
                    int((digest_done-backup_done)*1000), int((time.perf_counter()-started)*1000),
                )
                return self.version
            raw_segments = [
                memoryview(raw)[offset:offset + _RAW_SNAPSHOT_SEGMENT_BYTES]
                for offset in range(0, len(raw), _RAW_SNAPSHOT_SEGMENT_BYTES)
            ] or [memoryview(raw)]
            snapshot_record_sets, segment_fingerprints, compressed_bytes = (
                _prepare_snapshot_segment_records(
                    raw_segments,
                    base_fingerprints=self._base_snapshot_segment_fingerprints,
                    base_records=self._base_snapshot_record_sets,
                )
            )
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
        if (len(snapshot_record_sets) > _MAX_SNAPSHOT_SEGMENTS
                or any(_snapshot_record_compressed_bytes(records)
                       > _MAX_COMPRESSED_SEGMENT_BYTES
                       for records in snapshot_record_sets)):
            raise CollectionSnapshotUnavailable(
                "Collections snapshot exceeds its storage limit",
                failure_reason="COMPRESSED_SNAPSHOT_TOO_LARGE",
                database_size_bytes=len(raw),
                compressed_size_bytes=compressed_bytes,
            )

        now = _utc(self._utcnow())
        digest = hashlib.sha256(raw).hexdigest()
        summary_pointer = self._latest_dataset_summary_metadata()
        next_version = self.version + 1
        dashboard_records = self._dashboard_bootstrap_records()
        record_sets = [*snapshot_record_sets]
        if dashboard_records is not None:
            record_sets.append(dashboard_records)
        upload_started = time.perf_counter()
        try:
            # Immutable SQLite segments and the optional dashboard read model share
            # bounded transactions. The SQLite pointer remains the publication
            # boundary; a failed optional model is retried without that model.
            try:
                references = self._blobs.put_many(
                    record_sets,
                    now=now,
                    retain_until=now + _RETENTION,
                    binary_chunks=True,
                )
            except Exception as error:
                if dashboard_records is None:
                    raise
                _logger.warning(
                    "Firestore dashboard bootstrap omitted error_type=%s",
                    _exception_class(error),
                )
                references = self._blobs.put_many(
                    snapshot_record_sets,
                    now=now,
                    retain_until=now + _RETENTION,
                    binary_chunks=True,
                )
            upload_done = time.perf_counter()
        except Exception as error:
            raise CollectionSnapshotUnavailable(
                "Collections snapshot could not be uploaded",
                failure_reason="BLOB_UPLOAD_FAILED", cause_class=_exception_class(error),
            ) from None
        snapshot_references = references[:len(snapshot_record_sets)]
        dashboard_pointer = (
            self._dashboard_bootstrap_pointer(
                references[-1], snapshot_version=next_version, sqlite_sha256=digest,
            )
            if dashboard_records is not None and len(references) > len(snapshot_record_sets)
            else {}
        )
        snapshot_pointer = ({"snapshot_ref": snapshot_references[0]}
                            if len(snapshot_references) == 1 else
                            {"snapshot_refs": snapshot_references})
        updated = {"schema": 1, "version": next_version,
                   **snapshot_pointer,
                   "sqlite_snapshot_encoding": "sqlite-segment-gzip-base64-v1",
                   "sqlite_sha256": digest,
                   "sqlite_content_sha256": content_digest,
                   "updated_at": now.isoformat(), **summary_pointer,
                   **dashboard_pointer}

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
        self._dashboard_bootstrap_present = bool(dashboard_pointer)
        self._dashboard_bootstrap_checked = True
        self._base_snapshot_segment_fingerprints = segment_fingerprints
        self._base_snapshot_record_sets = tuple(snapshot_record_sets)
        finished = time.perf_counter()
        _logger.info(
            "Collections snapshot timing unchanged=false database_bytes=%d compressed_bytes=%d segments=%d "
            "backup_ms=%d digest_ms=%d compression_ms=%d preparation_ms=%d upload_ms=%d commit_ms=%d total_ms=%d",
            len(raw), compressed_bytes, len(snapshot_references), int((backup_done-started)*1000),
            int((digest_done-backup_done)*1000), int((compression_done-digest_done)*1000),
            int((upload_started-compression_done)*1000), int((upload_done-upload_started)*1000),
            int((finished-upload_done)*1000), int((finished-started)*1000),
        )
        return self.version

    def _backfill_latest_dataset_summary(self, summary_pointer: dict) -> None:
        """Compatibility wrapper for the version-fenced read-model backfill."""
        self._backfill_optional_metadata(summary_pointer)

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
