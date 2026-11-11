"""Typed query-source and provenance contracts, separate from Wikimedia observations."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from app.sources.youtube.errors import YouTubeSourceError


@dataclass(frozen=True)
class QueryContext:
    plan_id: str
    plan_version: int
    plan_fingerprint: str
    query_fingerprint: str
    canonical_query: str
    taxonomy_scope_label: str
    source_id: str
    provider_id: str
    region_code: str
    relevance_language: str
    batch_started_at: datetime
    published_after: datetime

    @property
    def context_key(self) -> str:
        """Opaque binding for a page token to the exact query and time-filter context."""
        payload = {
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_fingerprint": self.plan_fingerprint,
            "query_fingerprint": self.query_fingerprint,
            "query": self.canonical_query,
            "scope": self.taxonomy_scope_label,
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "region_code": self.region_code,
            "relevance_language": self.relevance_language,
            "published_after": format_utc(self.published_after),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, repr=False)
class ScopedPageToken:
    value: str
    context_key: str

    def __repr__(self) -> str:
        return f"ScopedPageToken(value='<redacted>', context_key={self.context_key!r})"


@dataclass(frozen=True)
class QueryExecutionPlan:
    plan_id: str
    plan_version: int
    plan_fingerprint: str
    batch_started_at: datetime
    published_after: datetime
    queries: tuple[QueryContext, ...]


@dataclass(frozen=True)
class MetricDescriptor:
    name: str
    unit: str
    meaning: str
    value: int
    classification: str = "PROVIDER_SEARCH_POSITION"


@dataclass(frozen=True)
class QueryEvidence:
    source_id: str
    provider_id: str
    query_plan_id: str
    query_plan_version: int
    query_plan_fingerprint: str
    query_fingerprint: str
    canonical_query: str
    taxonomy_scope_label: str
    market_filter: str
    language_filter: str
    published_after: datetime
    fetched_at: datetime
    requested_page: int
    result_rank: int
    page_index: int
    provider_response_status: int


@dataclass(frozen=True)
class YouTubeSearchRecord:
    source_id: str
    provider_id: str
    data_mode: str
    record_schema_version: int
    provider_video_id: str
    canonical_video_url: str
    title: str | None
    description: str | None
    channel_id: str | None
    channel_title: str | None
    published_at: datetime | None
    fetched_at: datetime
    query_plan_id: str
    query_plan_version: int
    query_fingerprint: str
    query_evidence: tuple[QueryEvidence, ...]
    market_filter: str
    language_filter: str
    result_rank: int
    page_index: int
    metric: MetricDescriptor
    quality_flags: tuple[str, ...]
    provider_response_status: int
    thumbnail_urls: tuple[str, ...] = ()
    live_broadcast_content: str | None = None


@dataclass(frozen=True)
class ProviderPage:
    items: tuple[object, ...]
    next_page_token: str | None
    estimated_total_results: int | None
    response_status: int


@dataclass(frozen=True)
class QueryExecutionResult:
    query: QueryContext
    requested_pages: int
    returned_count: int
    accepted_count: int
    rejected_count: int
    rejection_reasons: tuple[str, ...]
    pagination_state: str
    stop_reason: str
    error: YouTubeSourceError | None = None


@dataclass(frozen=True)
class RequestBudgetSummary:
    planned_calls: int
    max_requests: int
    attempted_calls: int
    completed_calls: int
    failed_calls: int
    retried_calls: int
    pages_fetched: int
    remaining_budget: int
    stop_reason: str | None


@dataclass(frozen=True)
class QueryBatchResult:
    status: str
    complete: bool
    data_mode: str
    source_id: str
    provider_id: str
    query_plan_id: str
    query_plan_version: int
    query_plan_fingerprint: str
    batch_started_at: datetime
    published_after: datetime
    records: tuple[YouTubeSearchRecord, ...]
    queries: tuple[QueryExecutionResult, ...]
    budget: RequestBudgetSummary
    stop_reason: str | None


def format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
