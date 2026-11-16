"""Focused expiration for persisted YouTube API evidence."""

import re
from datetime import datetime, timezone

from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.sources.catalog import YOUTUBE_SOURCE_ID
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_sqlite_snapshot_blobs import LocalSQLiteSnapshotBlobs

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def purge_expired_youtube_cache(
    cache: SharedCache,
    scope: CacheScope,
    grant: CacheGrant,
    *,
    now: datetime,
) -> bool:
    """Delete an expired or invalid YouTube cache entry and nothing else."""
    if (not isinstance(cache, SharedCache)
            or not isinstance(scope, CacheScope)
            or scope.source_id != YOUTUBE_SOURCE_ID
            or not isinstance(grant, CacheGrant)
            or grant.source_id != YOUTUBE_SOURCE_ID
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None):
        raise ValueError("YouTube cache scope, grant and aware time are required")

    result = cache.get(scope, grant=grant, now=now)
    if (result.get("status") in {"EXPIRED", "REJECTED"}
            or result.get("reason") == "POLICY_REVOKED"):
        cache.delete(scope)
        return True
    return False


def purge_expired_youtube_checkpoints(
    store: LocalSQLiteAtomicStateStore,
    snapshot_blobs: LocalSQLiteSnapshotBlobs,
    *,
    now: datetime,
    state_key: str,
) -> dict[str, int]:
    """Remove only expired YouTube checkpoints and their expired, unshared blobs."""
    if (type(store) is not LocalSQLiteAtomicStateStore or store.data_mode != "LIVE"
            or type(snapshot_blobs) is not LocalSQLiteSnapshotBlobs
            or snapshot_blobs.store is not store
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or not isinstance(state_key, str) or not state_key):
        raise ValueError("local LIVE store, matching blob store, aware time and state key required")
    now = now.astimezone(timezone.utc)

    def operation(state):
        if not state:
            return state, {"expired_checkpoints": 0, "invalid_checkpoints": 0,
                           "deleted_blobs": 0, "retained_referenced_blobs": 0}
        jobs = state.get("jobs", {})
        if not isinstance(jobs, dict):
            raise ValueError("YouTube checkpoint job state is invalid")
        candidates = set()
        expired = invalid = 0
        for job in jobs.values():
            if not isinstance(job, dict):
                raise ValueError("YouTube checkpoint job is invalid")
            checkpoints = job.get("source_checkpoints", {})
            if not isinstance(checkpoints, dict):
                raise ValueError("source checkpoint map is invalid")
            entry = checkpoints.get(YOUTUBE_SOURCE_ID)
            if entry is None:
                continue
            if not isinstance(entry, dict):
                del checkpoints[YOUTUBE_SOURCE_ID]
                invalid += 1
                continue
            reference = entry.get("snapshot_ref")
            digest = reference.get("digest") if isinstance(reference, dict) else None
            try:
                retain_until = datetime.fromisoformat(entry["retain_until"])
                job_expires = datetime.fromisoformat(job["expires_at"])
                if (retain_until.tzinfo is None or retain_until.utcoffset() is None
                        or job_expires.tzinfo is None or job_expires.utcoffset() is None):
                    raise ValueError("naive checkpoint expiry")
                expiry = min(retain_until.astimezone(timezone.utc),
                             job_expires.astimezone(timezone.utc))
            except (KeyError, TypeError, ValueError):
                del checkpoints[YOUTUBE_SOURCE_ID]
                invalid += 1
                if isinstance(digest, str) and _SHA256.fullmatch(digest):
                    candidates.add(digest)
                continue
            if expiry <= now:
                del checkpoints[YOUTUBE_SOURCE_ID]
                expired += 1
                if isinstance(digest, str) and _SHA256.fullmatch(digest):
                    candidates.add(digest)
        return state, {"expired_checkpoints": expired,
                       "invalid_checkpoints": invalid,
                       "candidate_blob_digests": candidates}

    result = store.transact(state_key, operation)
    candidate_digests = result.pop("candidate_blob_digests", set())
    blob_result = snapshot_blobs.delete_expired_unreferenced(
        candidate_digests, now=now
    )
    result.update(deleted_blobs=blob_result["deleted"],
                  retained_referenced_blobs=blob_result["retained_referenced"])
    return result
