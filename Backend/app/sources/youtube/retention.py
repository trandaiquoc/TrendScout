"""Focused expiration for persisted YouTube API evidence."""

from datetime import datetime

from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.sources.catalog import YOUTUBE_SOURCE_ID


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
