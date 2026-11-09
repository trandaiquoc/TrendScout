"""Provisional source-independent observation and snapshot structures."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class FetchStatus(StrEnum):
    SUCCESS = "SUCCESS"
    DAY_UNAVAILABLE = "DAY_UNAVAILABLE"
    FETCH_ERROR = "FETCH_ERROR"


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
