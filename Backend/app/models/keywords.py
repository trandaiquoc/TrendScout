"""Keyword results retain both entity identity and per-day provenance."""

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class ClassificationEvidence:
    category: str
    rule_id: str
    matched_text: str


@dataclass(frozen=True)
class KeywordResult:
    keyword_id: str
    observation_id: str
    canonical_keyword: str
    display_keyword: str
    search_keyword: str
    project: str
    article_original: str
    categories: tuple[str, ...]
    classification_version: str
    classification_evidence: tuple[ClassificationEvidence, ...]
    observation_date: date
    source: str
    country_scope: str
    access: str
    fetched_at: datetime | None
    views_ceil: int | None
    rank: int | None
    source_url: str | None
    quality_flags: tuple[str, ...]
    raw_record: dict[str, Any] | None

    def to_record(self) -> dict[str, Any]:
        """Return a Firestore-compatible mapping without losing evidence."""
        record = asdict(self)
        record["observation_date"] = self.observation_date.isoformat()
        record["categories"] = list(self.categories)
        record["quality_flags"] = list(self.quality_flags)
        record["classification_evidence"] = [asdict(item) for item in self.classification_evidence]
        return record
