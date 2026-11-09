"""Full-title keyword candidates and transparent, conservative topic rules."""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from app.models.keywords import KeywordResult
from app.taxonomy.legacy import CLASSIFICATION_VERSION, classify_legacy
from app.models.observations import PageObservation

def _id(parts: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def extract_keyword(observation: PageObservation) -> KeywordResult:
    title = observation.canonical_keyword
    if not isinstance(title, str) or not title.strip():
        raise ValueError("A cleaned canonical keyword is required")
    # Search representation is not an entity identifier; distinct records stay distinct.
    search = " ".join(unicodedata.normalize("NFC", title).casefold().split())
    categories, evidence = classify_legacy(title)
    flags = observation.quality_flags + ("TITLE_RULES_UNVERIFIED",)
    if categories == ("unclassified",):
        flags += ("INSUFFICIENT_TOPIC_EVIDENCE",)
    return KeywordResult(
        keyword_id=_id((observation.source, observation.project, observation.article_original)),
        observation_id=_id((observation.source, observation.country_scope, observation.access, observation.project,
                            observation.article_original, observation.observation_date.isoformat())),
        canonical_keyword=title, display_keyword=observation.article_display, search_keyword=search,
        project=observation.project, article_original=observation.article_original,
        categories=categories, classification_version=CLASSIFICATION_VERSION,
        classification_evidence=tuple(evidence), observation_date=observation.observation_date,
        source=observation.source, country_scope=observation.country_scope, access=observation.access,
        fetched_at=observation.fetched_at, views_ceil=observation.views_ceil, rank=observation.provider_rank,
        source_url=observation.source_url, quality_flags=flags, raw_record=observation.raw_record,
    )


def extract_keywords(observations: Iterable[PageObservation]) -> list[KeywordResult]:
    return [extract_keyword(row) for row in observations]


@dataclass(frozen=True)
class FilteredKeywords:
    items: tuple[KeywordResult, ...]
    empty_reason: str | None


def filter_keywords(results: Iterable[KeywordResult], category: str = "all") -> FilteredKeywords:
    if category not in {"all", "fashion", "home_goods", "other", "unclassified"}:
        raise ValueError("Unknown category")
    items = tuple(result for result in results if category == "all" or category in result.categories)
    return FilteredKeywords(items, None if items else "NO_MATCHES_IN_ANALYZED_DATA")
