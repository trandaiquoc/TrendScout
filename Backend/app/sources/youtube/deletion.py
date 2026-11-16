"""Operator-verified deletion of locally stored YouTube API evidence."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.jobs.shared_cache import CacheScope
from app.normal_wikimedia_live_policy import GOOGLE_STATE_KEY
from app.sources.catalog import YOUTUBE_SOURCE_ID
from app.storage.local_file_security import UnsafeLocalFile, verify_private_file
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore

DELETION_STATE_KEY = "youtube-deletion-v1"
_TICKET = re.compile(r"^[A-Za-z0-9_-]{24,64}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MAX_REQUESTS = 512


class YouTubeDeletionError(RuntimeError):
    """A deletion request cannot be safely completed with the available state."""


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


def _ticket_hash(reference: str) -> str:
    if not isinstance(reference, str) or not _TICKET.fullmatch(reference):
        raise ValueError("valid opaque deletion reference required")
    return hashlib.sha256(reference.encode("ascii")).hexdigest()


def _source_blob_references(value) -> set[str]:
    found = set()
    if isinstance(value, dict):
        reference = value.get("snapshot_ref")
        digest = reference.get("digest") if isinstance(reference, dict) else None
        if isinstance(digest, str) and _SHA256.fullmatch(digest):
            found.add(digest)
        for child in value.values():
            found.update(_source_blob_references(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_source_blob_references(child))
    return found


def _has_active_youtube_job(state: dict, now: datetime) -> bool:
    jobs = state.get("jobs", {})
    if not isinstance(jobs, dict):
        raise YouTubeDeletionError("stored job state is invalid; no data was changed")
    for job in jobs.values():
        if not isinstance(job, dict) or job.get("state") not in {"QUEUED", "RUNNING"}:
            continue
        metadata = job.get("request_metadata", {})
        sources = metadata.get("sources") if isinstance(metadata, dict) else None
        if isinstance(sources, list) and YOUTUBE_SOURCE_ID not in sources:
            continue
        try:
            expires_at = _utc(datetime.fromisoformat(job["expires_at"]))
        except (KeyError, TypeError, ValueError):
            raise YouTubeDeletionError(
                "an active Fetch has invalid expiry metadata; inspect it before deletion"
            ) from None
        if expires_at > now:
            return True
    return False


def inspect_youtube_summaries(path: str | Path | None) -> dict[str, int]:
    """Count source-specific Fetch summaries without modifying the database."""
    empty = {"youtube_dataset_snapshots": 0, "youtube_only_snapshots": 0,
             "youtube_fetch_jobs": 0}
    if path is None or not Path(path).is_file():
        return empty
    database = Path(path)
    if database.is_symlink():
        raise YouTubeDeletionError("Collections database must be a regular local file")
    try:
        verify_private_file(database)
    except UnsafeLocalFile:
        raise YouTubeDeletionError("Collections database is not private; no data was changed") from None
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "dataset_snapshots" not in tables:
            return empty
        result = dict(empty)
        for dataset_id, serialized in connection.execute(
                "SELECT dataset_id,source_manifest FROM dataset_snapshots"):
            try:
                manifest = json.loads(serialized)
            except (TypeError, json.JSONDecodeError):
                raise YouTubeDeletionError(
                    "a Fetch source manifest is invalid; review it before deletion"
                ) from None
            if not isinstance(manifest, list) or any(not isinstance(item, dict) for item in manifest):
                raise YouTubeDeletionError(
                    "a Fetch source manifest is invalid; review it before deletion"
                )
            youtube_entries = [item for item in manifest
                               if item.get("source_id") == YOUTUBE_SOURCE_ID]
            if not youtube_entries:
                continue
            result["youtube_dataset_snapshots"] += 1
            if len(youtube_entries) == len(manifest):
                result["youtube_only_snapshots"] += 1
                if "fetch_jobs" in tables:
                    found = connection.execute(
                        "SELECT 1 FROM fetch_jobs WHERE fetch_id=?", (dataset_id,)
                    ).fetchone()
                    result["youtube_fetch_jobs"] += int(found is not None)
        return result
    finally:
        connection.close()


def purge_youtube_summaries(path: str | Path | None) -> dict[str, int]:
    """Remove YouTube-only snapshot metadata while preserving mixed-source history."""
    result = {"youtube_dataset_snapshots_redacted": 0,
              "youtube_only_snapshots_removed": 0,
              "youtube_fetch_jobs_removed": 0}
    if path is None or not Path(path).is_file():
        return result
    database = Path(path)
    if database.is_symlink():
        raise YouTubeDeletionError("Collections database must be a regular local file")
    try:
        verify_private_file(database)
    except UnsafeLocalFile:
        raise YouTubeDeletionError("Collections database is not private; no data was changed") from None
    connection = sqlite3.connect(database, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "dataset_snapshots" not in tables:
            return result
        connection.execute("BEGIN IMMEDIATE")
        changes = []
        for row in connection.execute(
                "SELECT dataset_id,status,source_manifest,provider_record_count,"
                "received_record_count,quality_flags FROM dataset_snapshots"):
            try:
                manifest = json.loads(row["source_manifest"])
                flags = json.loads(row["quality_flags"])
            except (TypeError, json.JSONDecodeError):
                raise YouTubeDeletionError(
                    "a Fetch summary is invalid; no summary changes were committed"
                ) from None
            if (not isinstance(manifest, list)
                    or any(not isinstance(item, dict) for item in manifest)
                    or not isinstance(flags, list)
                    or any(not isinstance(flag, str) for flag in flags)):
                raise YouTubeDeletionError(
                    "a Fetch summary is invalid; no summary changes were committed"
                )
            youtube_entries = [item for item in manifest
                               if item.get("source_id") == YOUTUBE_SOURCE_ID]
            if not youtube_entries:
                continue
            remaining = [item for item in manifest
                         if item.get("source_id") != YOUTUBE_SOURCE_ID]
            if not remaining:
                changes.append(("DELETE", row["dataset_id"], 0, 0, [], ""))
                continue
            all_entries = [*remaining, *youtube_entries]
            for item in all_entries:
                if (type(item.get("record_count")) is not int or item["record_count"] < 0
                        or type(item.get("received_record_count")) is not int
                        or item["received_record_count"] < 0):
                    raise YouTubeDeletionError(
                        "a mixed Fetch summary lacks source-level counts; preserve it for manual review"
                    )
            provider_count = sum(item["record_count"] for item in remaining)
            received_count = sum(item["received_record_count"] for item in remaining)
            yt_received = sum(item["received_record_count"] for item in youtube_entries)
            statuses = [item.get("status") for item in remaining]
            remaining_flags = [flag for flag in flags if not flag.startswith("YOUTUBE_")]
            has_success = any(status in {"SUCCESS", "PARTIAL"} for status in statuses)
            if not has_success:
                status = "FAILED"
            elif any(item != "SUCCESS" for item in statuses) or remaining_flags:
                status = "PARTIAL"
            else:
                status = "COMPLETED"
            changes.append(("UPDATE", row["dataset_id"], provider_count,
                            received_count, remaining_flags, status, yt_received,
                            json.dumps(remaining, ensure_ascii=False, sort_keys=True)))

        for change in changes:
            if change[0] == "DELETE":
                _, dataset_id, *_ = change
                connection.execute("DELETE FROM dataset_snapshots WHERE dataset_id=?",
                                   (dataset_id,))
                if "fetch_jobs" in tables:
                    cursor = connection.execute("DELETE FROM fetch_jobs WHERE fetch_id=?",
                                                (dataset_id,))
                    result["youtube_fetch_jobs_removed"] += cursor.rowcount
                result["youtube_only_snapshots_removed"] += 1
                continue
            (_, dataset_id, provider_count, received_count, flags,
             status, yt_received, manifest) = change
            connection.execute(
                "UPDATE dataset_snapshots SET status=?,source_manifest=?,"
                "provider_record_count=?,received_record_count=?,quality_flags=? "
                "WHERE dataset_id=?",
                (status, manifest, provider_count, received_count,
                 json.dumps(flags, ensure_ascii=False), dataset_id),
            )
            if "fetch_jobs" in tables:
                fetch_job = connection.execute(
                    "SELECT available FROM fetch_jobs WHERE fetch_id=?", (dataset_id,)
                ).fetchone()
                if fetch_job is not None:
                    available = fetch_job["available"]
                    if type(available) is not int or available < yt_received:
                        raise YouTubeDeletionError(
                            "Fetch history counts do not match the source manifest; review manually"
                        )
                    connection.execute(
                        "UPDATE fetch_jobs SET available=? WHERE fetch_id=?",
                        (available - yt_received, dataset_id),
                    )
            result["youtube_dataset_snapshots_redacted"] += 1
        connection.commit()
        return result
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


class LocalYouTubeDeletionWorkflow:
    """A local operator-only request log and idempotent deletion boundary.

    Requester identity verification happens outside the application. Only an
    opaque ticket hash, timestamps, and deletion counts are retained here.
    """

    def __init__(self, store: LocalSQLiteAtomicStateStore, *, cache_scope: CacheScope,
                 collection_database: str | Path | None = None,
                 state_key: str = GOOGLE_STATE_KEY):
        if (type(store) is not LocalSQLiteAtomicStateStore or store.data_mode != "LIVE"
                or not isinstance(cache_scope, CacheScope)
                or cache_scope.source_id != YOUTUBE_SOURCE_ID
                or not isinstance(state_key, str) or not state_key):
            raise ValueError("local LIVE store and YouTube cache scope are required")
        self.store = store
        self.cache_scope = cache_scope
        self.collection_database = (Path(collection_database)
                                    if collection_database is not None else None)
        self.state_key = state_key

    def receive_request(self, *, now: datetime) -> str:
        received_at = _utc(now)
        reference = secrets.token_urlsafe(24)
        digest = hashlib.sha256(reference.encode("ascii")).hexdigest()

        def operation(state):
            if not state:
                state = {"schema": 1, "requests": {}}
            requests = state.get("requests")
            if state.get("schema") != 1 or not isinstance(requests, dict):
                raise YouTubeDeletionError("deletion request ledger is invalid")
            if digest in requests or len(requests) >= _MAX_REQUESTS:
                raise YouTubeDeletionError("deletion request ledger capacity is unavailable")
            requests[digest] = {"status": "PENDING", "received_at": received_at.isoformat()}
            return state, None

        self.store.transact(DELETION_STATE_KEY, operation)
        return reference

    def preview(self, *, now: datetime) -> dict[str, object]:
        checked_at = _utc(now)
        connection = sqlite3.connect(self.store.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = dict(connection.execute("SELECT state_key,state_json FROM atomic_state"))
        finally:
            connection.close()
        state = json.loads(rows[ self.state_key ]) if self.state_key in rows else {}
        if not isinstance(state, dict):
            raise YouTubeDeletionError("stored job state is invalid")
        checkpoints = 0
        jobs = state.get("jobs", {})
        if not isinstance(jobs, dict):
            raise YouTubeDeletionError("stored job state is invalid")
        for job in jobs.values():
            checkpoint_map = job.get("source_checkpoints", {}) if isinstance(job, dict) else None
            if isinstance(checkpoint_map, dict) and YOUTUBE_SOURCE_ID in checkpoint_map:
                checkpoints += 1
        return {
            "checked_at": checked_at.isoformat(),
            "youtube_cache_entry": int(self.cache_scope.key in rows),
            "youtube_checkpoints": checkpoints,
            "active_youtube_fetch": _has_active_youtube_job(state, checked_at),
            "collection_summaries": inspect_youtube_summaries(self.collection_database),
        }

    def fulfill_request(self, reference: str, *, requester_verified: bool,
                        application_stopped: bool, now: datetime) -> dict[str, object]:
        completed_at = _utc(now)
        if requester_verified is not True:
            raise PermissionError("operator verification of the requester is required")
        if application_stopped is not True:
            raise PermissionError("the local TrendScout application must be stopped")
        digest = _ticket_hash(reference)
        state_counts = self._purge_atomic_youtube_state(digest, completed_at)
        if "external_youtube_data_deleted" in state_counts:
            return state_counts
        summary_counts = purge_youtube_summaries(self.collection_database)
        self._complete_request(digest, summary_counts, completed_at)
        return {**state_counts, **summary_counts,
                "external_youtube_data_deleted": False,
                "browser_storage_requires_user_action": True}

    def _purge_atomic_youtube_state(self, digest: str, now: datetime) -> dict[str, int]:
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                request_row = connection.execute(
                    "SELECT state_json FROM atomic_state WHERE state_key=?",
                    (DELETION_STATE_KEY,),
                ).fetchone()
                if request_row is None:
                    raise YouTubeDeletionError("deletion request was not received")
                request_state = json.loads(request_row[0])
                if (not isinstance(request_state, dict)
                        or request_state.get("schema") != 1
                        or not isinstance(request_state.get("requests"), dict)):
                    raise YouTubeDeletionError("deletion request ledger is invalid")
                request = request_state["requests"].get(digest)
                if not isinstance(request, dict):
                    raise YouTubeDeletionError("deletion request reference is unknown")
                if request.get("status") == "COMPLETED":
                    connection.rollback()
                    return dict(request.get("result", {}))
                if request.get("status") not in {"PENDING", "STATE_PURGED"}:
                    raise YouTubeDeletionError("deletion request state is invalid")

                state_row = connection.execute(
                    "SELECT state_json FROM atomic_state WHERE state_key=?",
                    (self.state_key,),
                ).fetchone()
                state = json.loads(state_row[0]) if state_row else {}
                if not isinstance(state, dict):
                    raise YouTubeDeletionError("stored job state is invalid")
                if request.get("status") == "PENDING" and _has_active_youtube_job(state, now):
                    raise YouTubeDeletionError(
                        "a YouTube Fetch is queued or running; stop it before deletion"
                    )
                if request.get("status") == "STATE_PURGED":
                    connection.rollback()
                    return dict(request.get("state_purge", {}))

                jobs = state.get("jobs", {})
                if not isinstance(jobs, dict):
                    raise YouTubeDeletionError("stored job state is invalid")
                blob_candidates = set()
                checkpoints_removed = 0
                for job in jobs.values():
                    if not isinstance(job, dict):
                        raise YouTubeDeletionError("stored job record is invalid")
                    checkpoints = job.get("source_checkpoints", {})
                    if not isinstance(checkpoints, dict):
                        raise YouTubeDeletionError("stored checkpoint map is invalid")
                    entry = checkpoints.get(YOUTUBE_SOURCE_ID)
                    if entry is not None:
                        reference_item = entry.get("snapshot_ref") if isinstance(entry, dict) else None
                        blob_digest = (reference_item.get("digest")
                                       if isinstance(reference_item, dict) else None)
                        storage_mode = entry.get("snapshot_storage", "INLINE") if isinstance(entry, dict) else None
                        if (storage_mode == "EXTERNAL_RECORDS"
                                and (not isinstance(blob_digest, str)
                                     or not _SHA256.fullmatch(blob_digest))):
                            raise YouTubeDeletionError(
                                "a YouTube checkpoint blob reference is invalid; review it manually"
                            )
                        if blob_digest is not None:
                            if not isinstance(blob_digest, str) or not _SHA256.fullmatch(blob_digest):
                                raise YouTubeDeletionError(
                                    "a YouTube checkpoint blob reference is invalid; review it manually"
                                )
                            blob_candidates.add(blob_digest)
                        del checkpoints[YOUTUBE_SOURCE_ID]
                        checkpoints_removed += 1

                if state_row is not None and checkpoints_removed:
                    serialized = json.dumps(state, ensure_ascii=False, allow_nan=False,
                                            separators=(",", ":"), sort_keys=True)
                    connection.execute(
                        "UPDATE atomic_state SET state_json=? WHERE state_key=?",
                        (serialized, self.state_key),
                    )

                cache_cursor = connection.execute(
                    "DELETE FROM atomic_state WHERE state_key=?", (self.cache_scope.key,)
                )
                rows = connection.execute(
                    "SELECT state_key,state_json FROM atomic_state"
                ).fetchall()
                references = set()
                for _, serialized in rows:
                    value = json.loads(serialized)
                    if not isinstance(value, dict):
                        raise YouTubeDeletionError("stored atomic state is invalid")
                    references.update(_source_blob_references(value))
                retained_digests = blob_candidates & references
                deleted_blobs = 0
                if blob_candidates - retained_digests:
                    table = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='source_snapshot_blobs'"
                    ).fetchone()
                    if table is None and blob_candidates:
                        raise YouTubeDeletionError(
                            "YouTube checkpoint blobs are unavailable; deletion needs manual review"
                        )
                    for blob_digest in blob_candidates - retained_digests:
                        cursor = connection.execute(
                            "DELETE FROM source_snapshot_blobs WHERE digest=?",
                            (blob_digest,),
                        )
                        deleted_blobs += cursor.rowcount

                state_counts = {
                    "youtube_cache_entries_removed": cache_cursor.rowcount,
                    "youtube_checkpoints_removed": checkpoints_removed,
                    "youtube_snapshot_blobs_removed": deleted_blobs,
                    "shared_snapshot_blobs_preserved": len(retained_digests),
                }
                request.update(status="STATE_PURGED", state_purge=state_counts,
                               state_purged_at=now.isoformat())
                request_state["requests"][digest] = request
                connection.execute(
                    "UPDATE atomic_state SET state_json=? WHERE state_key=?",
                    (json.dumps(request_state, ensure_ascii=False, allow_nan=False,
                                separators=(",", ":"), sort_keys=True), DELETION_STATE_KEY),
                )
                connection.commit()
                return state_counts
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def _complete_request(self, digest, summary_counts, now):
        def operation(state):
            requests = state.get("requests") if isinstance(state, dict) else None
            request = requests.get(digest) if isinstance(requests, dict) else None
            if not isinstance(request, dict) or request.get("status") not in {
                    "STATE_PURGED", "COMPLETED"}:
                raise YouTubeDeletionError("deletion request could not be finalized")
            if request.get("status") == "COMPLETED":
                return state, None
            request.update(status="COMPLETED", summary_purge=summary_counts,
                           completed_at=now.isoformat())
            request["result"] = {**request.get("state_purge", {}), **summary_counts,
                                  "external_youtube_data_deleted": False,
                                  "browser_storage_requires_user_action": True}
            return state, None

        self.store.transact(DELETION_STATE_KEY, operation)
