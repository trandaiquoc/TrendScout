"""Full-title keyword candidates and transparent, conservative topic rules."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from app.models.keywords import ClassificationEvidence, KeywordResult
from app.models.observations import PageObservation

CLASSIFICATION_VERSION = "title-rules-v0.1"
# Specific phrases and contextual signals, not generic 'dress', 'home', 'brand',
# 'fashion' or 'designer' by themselves. Rules are initial hypotheses, not labels
# verified against article contents or an annotated evaluation set.
_RULES = (
    ("fashion", "fashion_industry", r"\b(?:fashion industry|fashion design|fashion designer|fashion week|haute couture|fashion house|clothing brand|clothing company|clothing retailer|clothing manufacturer|clothing industry|wedding dress|wedding dresses|fashion retail)\b"),
    ("home_goods", "home_products", r"\b(?:home appliance|home appliances|household appliance|household appliances|kitchen appliance|kitchen appliances|vacuum cleaner|vacuum cleaners|washing machine|washing machines|dishwasher|dishwashers|air fryer|air fryers|cookware|furniture manufacturer|furniture retailer|home furnishings)\b"),
    ("other", "elections", r"\b(?:presidential election|general election|mayoral election|parliamentary election|senate election)\b"),
    ("other", "film_or_television", r"\((?:\d{4}\s+)?(?:film|television series|tv series|tv show)\)\s*$"),
    ("other", "professional_sport", r"\b(?:national football league|major league baseball|national basketball association|world series)\b"),
)
_COMPILED = tuple((cat, rule, re.compile(pattern, re.IGNORECASE)) for cat, rule, pattern in _RULES)
_MEDIA_QUALIFIER = re.compile(r"\((?:\d{4}\s+)?(?:film|song|album|television series|tv series|tv show)\)\s*$", re.IGNORECASE)


def _id(parts: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def extract_keyword(observation: PageObservation) -> KeywordResult:
    title = observation.canonical_keyword
    if not isinstance(title, str) or not title.strip():
        raise ValueError("A cleaned canonical keyword is required")
    # Search representation is not an entity identifier; distinct records stay distinct.
    search = " ".join(unicodedata.normalize("NFC", title).casefold().split())
    evidence = []
    media_qualified = bool(_MEDIA_QUALIFIER.search(title))
    for category, rule_id, pattern in _COMPILED:
        if category in ("fashion", "home_goods") and media_qualified:
            continue
        for match in pattern.finditer(title):
            evidence.append(ClassificationEvidence(category, rule_id, match.group(0)))
    primary = {ev.category for ev in evidence if ev.category in ("fashion", "home_goods")}
    categories = tuple(cat for cat in ("fashion", "home_goods") if cat in primary)
    if not categories:
        categories = ("other",) if any(ev.category == "other" for ev in evidence) else ("unclassified",)
    else:
        evidence = [ev for ev in evidence if ev.category in primary]
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
