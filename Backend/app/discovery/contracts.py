"""Typed observations and assessments; provider metrics are never combined."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

DataMode = Literal["SYNTHETIC", "LIVE"]
MarketEvidence = Literal["US_COUNTRY_SCOPED", "US_FILTER_ONLY", "UNKNOWN"]


@dataclass(frozen=True)
class Observation:
    source_id: str
    provider_id: str
    document_id: str
    text: str
    observed_at: datetime
    fetched_at: datetime
    scope: str
    query_evidence: tuple[str, ...]
    market_evidence: MarketEvidence
    data_mode: DataMode
    parser_version: str
    language: str = "en"
    source_url: str | None = None
    delivery_mode: Literal["FRESH", "CACHED"] = "FRESH"
    article: str | None = None
    project: str | None = None
    provider_rank: int | None = None
    approximate_views: int | None = None


@dataclass(frozen=True)
class ProcessingGrant:
    """Explicit server-side grant; API access alone does not grant derived analytics."""

    source_id: str
    extraction_allowed: bool
    derived_metrics_allowed: bool
    expires_at: datetime
    policy_version: str


@dataclass(frozen=True)
class CoverageDay:
    source_id: str
    scope: str
    parser_version: str
    data_mode: DataMode
    day: date
    status: Literal["COMPLETE", "PARTIAL", "UNAVAILABLE"]
    eligible_documents: int | None
    absence_observable_in_sample: bool


@dataclass(frozen=True)
class Evidence:
    source_id: str
    provider_id: str
    document_id: str
    content_fingerprint: str
    observed_at: datetime
    fetched_at: datetime
    scope: str
    query_evidence: tuple[str, ...]
    market_evidence: MarketEvidence
    data_mode: DataMode
    parser_version: str
    source_url: str | None
    delivery_mode: Literal["FRESH", "CACHED"] = "FRESH"
    language: str = "en"
    article: str | None = None
    project: str | None = None
    provider_rank: int | None = None
    approximate_views: int | None = None


@dataclass(frozen=True)
class Candidate:
    keyword_id: str
    keyword: str
    original_forms: tuple[str, ...]
    categories: tuple[str, ...]
    taxonomy_version: str
    extraction_version: str
    quality_flags: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    classification_version: str = "topic-terms-v2"


@dataclass(frozen=True)
class Assessment:
    keyword_id: str
    source_id: str
    scope: str
    parser_version: str
    data_mode: DataMode
    market_evidence: MarketEvidence
    as_of: date
    classification: str
    confidence: str
    current_support: int | None
    baseline_support: int | None
    current_exposure: int | None
    baseline_exposure: int | None
    current_rate: float | None
    baseline_rate: float | None
    growth_ratio: float | None
    current_window: tuple[date, date]
    baseline_window: tuple[date, date]
    reason: str
    limitations: tuple[str, ...]
    algorithm_version: str = "sample-presence-v1"


@dataclass(frozen=True)
class Suggestion:
    status: Literal["SUGGESTION_FOR_RESEARCH", "NO_PRODUCT_SUGGESTION"]
    category: str | None
    reason: str
    confidence: str
    human_review_required: bool = True
