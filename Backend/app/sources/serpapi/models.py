"""Search evidence contracts; SERP position is not search volume or demand."""

from dataclasses import dataclass
from datetime import datetime

from app.sources.serpapi.quota import QuotaSnapshot


@dataclass(frozen=True)
class QueryContext:
    source_id: str
    provider_id: str
    plan_id: str
    plan_version: int
    plan_fingerprint: str
    query_fingerprint: str
    canonical_query: str
    taxonomy_scope: str
    started_at: datetime


@dataclass(frozen=True)
class ScopedOffset:
    value: int
    query_fingerprint: str


@dataclass(frozen=True)
class QueryEvidence:
    query: QueryContext
    fetched_at: datetime
    page: int
    offset: int
    position: int
    http_status: int
    cache_state: str
    provider_created_at: datetime | None


@dataclass(frozen=True)
class SearchRecord:
    source_id: str
    provider_id: str
    canonical_url: str
    title: str | None
    snippet: str | None
    fetched_at: datetime
    evidence: tuple[QueryEvidence, ...]
    quality_flags: tuple[str, ...]
    data_mode: str = "SYNTHETIC"
    schema_version: int = 1


@dataclass(frozen=True)
class ProviderPage:
    items: tuple[object, ...]
    next_offset: ScopedOffset | None
    http_status: int
    cache_state: str
    provider_created_at: datetime | None
    pagination_error: str | None = None


@dataclass(frozen=True)
class QueryResult:
    context: QueryContext
    pages_fetched: int
    returned: int
    accepted: int
    rejected: int
    stop_reason: str
    complete: bool
    error_code: str | None = None
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class BatchResult:
    source_id: str
    provider_id: str
    plan_fingerprint: str
    status: str
    complete: bool
    records: tuple[SearchRecord, ...]
    queries: tuple[QueryResult, ...]
    quota: QuotaSnapshot
    stop_reason: str | None
    data_mode: str = "SYNTHETIC"
