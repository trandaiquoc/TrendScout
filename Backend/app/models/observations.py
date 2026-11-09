"""Provisional source-independent observation and snapshot structures."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class FetchStatus(StrEnum):
    SUCCESS = "SUCCESS"
    DAY_UNAVAILABLE = "DAY_UNAVAILABLE"
    FETCH_ERROR = "FETCH_ERROR"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True)
class DatasetSnapshot:
    source: str
    country_scope: str
    access: str
    observation_date: date
    fetched_at: datetime
    fetch_status: FetchStatus
    http_status: int | None
    item_count: int | None
    request_url: str
    request_count: int = 0
    rejected_count: int | None = None
    error: str | None = None
    rejected_records: tuple[Any, ...] = ()


@dataclass(frozen=True)
class PageObservation:
    source: str
    country_scope: str
    access: str
    observation_date: date
    project: str
    article_original: str
    article_display: str
    views_ceil: int | None
    provider_rank: int | None
    quality_flags: tuple[str, ...] = ()
    canonical_keyword: str | None = None
    fetched_at: datetime | None = None
    source_url: str | None = None
    raw_record: dict[str, Any] | None = None

    @property
    def rank(self) -> int | None:
        return self.provider_rank

    def to_canonical(self) -> dict[str, Any]:
        return {
            "source": self.source, "country_scope": self.country_scope, "access": self.access,
            "project": self.project, "article_original": self.article_original,
            "canonical_keyword": self.canonical_keyword, "views_ceil": self.views_ceil,
            "rank": self.provider_rank, "observation_date": self.observation_date.isoformat(),
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "source_url": self.source_url, "quality_flags": list(self.quality_flags),
            "raw_record": self.raw_record,
        }


class ObservationStatus(StrEnum):
    OBSERVED = "OBSERVED"
    NOT_IN_TOP = "NOT_IN_TOP"
    DAY_UNAVAILABLE = "DAY_UNAVAILABLE"
    FETCH_ERROR = "FETCH_ERROR"


@dataclass(frozen=True)
class CollectionResult:
    """Collection coverage, including unsuccessful snapshots and provenance."""
    analysis_date: date | None
    days: tuple[tuple[DatasetSnapshot, list[PageObservation]], ...]

    @property
    def request_count(self) -> int:
        return sum(snapshot.request_count for snapshot, _ in self.days)
