"""Source-specific retention limits and review state for provider evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

from app.retention.policy import (
    DEFAULT_BUSINESS_TIMEZONE,
    DEFAULT_RETENTION_DAYS,
    effective_retention_deadline,
)
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_CONTEXT_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)

SOURCE_RETENTION_POLICY_VERSION = "source-retention-2026-11-26-v1"


@dataclass(frozen=True)
class SourceRetentionPolicy:
    source_id: str
    provider_max_age_days: int | None
    review_status: str
    scope: str

    def metadata(self) -> dict:
        return {
            "version": SOURCE_RETENTION_POLICY_VERSION,
            "provider_max_age_days": self.provider_max_age_days,
            "review_status": self.review_status,
            "scope": self.scope,
        }


_SOURCE_POLICIES = MappingProxyType({
    WIKIMEDIA_SOURCE_ID: SourceRetentionPolicy(
        source_id=WIKIMEDIA_SOURCE_ID,
        provider_max_age_days=None,
        review_status="reviewed",
        scope="Public Pageviews/API data; checked API guidance did not identify a shorter numeric client retention limit.",
    ),
    WIKIMEDIA_CONTEXT_SOURCE_ID: SourceRetentionPolicy(
        source_id=WIKIMEDIA_CONTEXT_SOURCE_ID,
        provider_max_age_days=None,
        review_status="partial",
        scope="Wikimedia/Wikidata-derived entity context; Wikidata-specific client retention terms still need review.",
    ),
    YOUTUBE_SOURCE_ID: SourceRetentionPolicy(
        source_id=YOUTUBE_SOURCE_ID,
        provider_max_age_days=30,
        review_status="reviewed",
        scope="General YouTube Search API data; no approved statistical-data exception is assumed.",
    ),
    GOOGLE_SOURCE_ID: SourceRetentionPolicy(
        source_id=GOOGLE_SOURCE_ID,
        provider_max_age_days=None,
        review_status="partial",
        scope="SerpApi service-side 31-day file expiry is not a client limit; Google Trends RSS terms still need review.",
    ),
    BING_SOURCE_ID: SourceRetentionPolicy(
        source_id=BING_SOURCE_ID,
        provider_max_age_days=None,
        review_status="partial",
        scope="SerpApi service-side 31-day file expiry is not a client limit; source-specific client terms still need review.",
    ),
})


def source_retention_policy(source_id: str) -> SourceRetentionPolicy:
    """Return an explicit source policy; unknown sources fail closed."""
    if not isinstance(source_id, str):
        raise ValueError("source retention policy requires a source ID")
    try:
        return _SOURCE_POLICIES[source_id]
    except KeyError:
        raise ValueError("source retention policy is not registered") from None


def effective_source_retention_deadline(
    source_id: str,
    collected_at: datetime,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    business_timezone: str = DEFAULT_BUSINESS_TIMEZONE,
) -> datetime:
    """Apply the stricter source limit and rolling local-calendar policy.

    A missing numeric provider limit means only that no shorter client limit
    was established in the reviewed source documents. It never lengthens or
    disables the application's rolling retention deadline.
    """
    policy = source_retention_policy(source_id)
    provider_expires_at = (
        collected_at + timedelta(days=policy.provider_max_age_days)
        if policy.provider_max_age_days is not None else None
    )
    return effective_retention_deadline(
        collected_at,
        provider_expires_at=provider_expires_at,
        retention_days=retention_days,
        business_timezone=business_timezone,
    )


def source_evidence_timestamp_status(source_id: str, collected_at, *, now=None) -> str:
    """Classify one evidence timestamp under the existing rolling retention policy."""
    if isinstance(collected_at, str) and len(collected_at) <= 64:
        try:
            collected_at = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        except ValueError:
            return "INVALID"
    if (not isinstance(collected_at, datetime) or collected_at.tzinfo is None
            or collected_at.utcoffset() is None):
        return "INVALID"
    evaluated_at = now or datetime.now(timezone.utc)
    if (not isinstance(evaluated_at, datetime) or evaluated_at.tzinfo is None
            or evaluated_at.utcoffset() is None):
        return "INVALID"
    collected_at = collected_at.astimezone(timezone.utc)
    evaluated_at = evaluated_at.astimezone(timezone.utc)
    if collected_at > evaluated_at:
        return "INVALID"
    try:
        deadline = effective_source_retention_deadline(source_id, collected_at)
    except ValueError:
        return "INVALID"
    return "EXPIRED" if deadline <= evaluated_at else "CURRENT"


def retained_live_wikimedia_evidence_status(source: dict, *, now=None) -> str:
    """Fail closed unless a fresh, retained Wikimedia checkpoint is provable."""
    from app.sources.catalog import WIKIMEDIA_SOURCE_ID

    if not isinstance(source, dict) or source.get("source_id") != WIKIMEDIA_SOURCE_ID:
        return "NOT_APPLICABLE"
    coverage = source.get("coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    if coverage.get("cache_only") is True:
        return "CACHE_ONLY"
    delivery = source.get("delivery_state")
    if delivery in {"CACHE_REPLAY", "CACHE_ONLY", "CACHED"}:
        return "CACHE_ONLY"
    if delivery in {"STALE_CACHE", "EXPIRED"}:
        return "EXPIRED"
    if (source.get("data_mode") != "LIVE"
            or source.get("status") not in {"SUCCESS", "PARTIAL"}
            or delivery != "FRESH"):
        return "INVALID"
    request_count = source.get("provider_request_count")
    if type(request_count) is not int or request_count < 0:
        request_count = coverage.get("provider_request_count")
    if type(request_count) is not int or request_count <= 0:
        return "INVALID"
    collected_at = (coverage.get("fetched_at") or source.get("fetched_at")
                    or coverage.get("source_fetched_at"))
    if collected_at is None:
        # Some adapters retain collection time on each record rather than on
        # the source manifest. The caller must validate every record timestamp.
        return "REQUIRES_RECORD_VALIDATION"
    return source_evidence_timestamp_status(WIKIMEDIA_SOURCE_ID, collected_at, now=now)
