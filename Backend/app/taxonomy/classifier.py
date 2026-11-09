"""Opt-in contextual rules; results do not alter legacy API or storage."""
from dataclasses import dataclass
import re
import unicodedata
from typing import Literal

from app.taxonomy.registry import TAXONOMY_VERSION, resolve

CLASSIFICATION_VERSION = 'context-title-rules-v0.2'
EntityType = Literal['person', 'organization', 'product', 'place', 'event', 'creative_work', 'brand', 'unknown']
CommercialRelevance = Literal['unknown', 'not_assessed']


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: str
    category_id: str
    subcategory_id: str | None
    pattern: str
    explanation: str
    priority: int = 10
    exclude_media_title: bool = False


@dataclass(frozen=True)
class RuleEvidence:
    rule_id: str
    rule_version: str
    category_id: str
    subcategory_id: str | None
    matched_text: str
    source: str
    project: str
    context: str
    explanation: str
    priority: int


@dataclass(frozen=True)
class ClassificationResult:
    keyword_id: str
    entity_type: EntityType
    topic_categories: tuple[str, ...]
    subcategories: tuple[str, ...]
    taxonomy_version: str
    classification_version: str
    rule_evidence: tuple[RuleEvidence, ...]
    classification_status: Literal['classified', 'unclassified']
    review_status: Literal['unreviewed']
    commercial_relevance: CommercialRelevance


RULES = (
    Rule('fashion_week', '1', 'fashion', None, r'\bfashion week\b', 'Explicit fashion-week phrase', exclude_media_title=True),
    Rule('washing_machine', '1', 'home_living', 'home_living.appliances', r'\bwashing machines?\b', 'Explicit appliance phrase', exclude_media_title=True),
    Rule('film_qualifier', '1', 'entertainment', 'entertainment.film', r'\((?:\d{4}\s+)?film\)\s*$', 'Explicit film qualifier', priority=100),
    Rule('election', '1', 'news_politics', 'news_politics.elections', r'\b(?:presidential|general|mayoral|parliamentary|senate) election\b', 'Explicit election phrase', exclude_media_title=True),
)
_MEDIA = re.compile(r'\((?:\d{4}\s+)?(?:film|song|album|television series|tv series|tv show)\)\s*$', re.IGNORECASE)
_COMPILED = tuple((rule, re.compile(rule.pattern, re.IGNORECASE)) for rule in RULES)


def classify(keyword_id: str, title: str, *, source: str, project: str) -> ClassificationResult:
    """English-title rules only on en.wikipedia; unknown context stays unclassified."""
    if not all(isinstance(x, str) and x.strip() for x in (keyword_id, title, source, project)):
        raise ValueError('keyword_id, title, source and project are required')
    title = unicodedata.normalize('NFC', title)
    evidence = []
    # Explicit source/project gating avoids applying English rules to arbitrary providers.
    if source == 'wikimedia_pageviews_top_per_country' and project == 'en.wikipedia':
        media = bool(_MEDIA.search(title))
        for rule, pattern in _COMPILED:
            resolve(rule.category_id)
            if rule.subcategory_id and resolve(rule.subcategory_id).parent_id != rule.category_id:
                raise ValueError('Rule subcategory does not match category')
            if media and rule.exclude_media_title:
                continue
            for match in pattern.finditer(title):
                evidence.append(RuleEvidence(rule.rule_id, rule.version, rule.category_id,
                    rule.subcategory_id, match.group(), source, project, 'article_title',
                    rule.explanation, rule.priority))
    evidence.sort(key=lambda e: (-e.priority, e.rule_id, e.matched_text))
    categories = tuple(dict.fromkeys(e.category_id for e in evidence)) or ('unclassified',)
    subcategories = tuple(dict.fromkeys(e.subcategory_id for e in evidence if e.subcategory_id))
    return ClassificationResult(keyword_id, 'unknown', categories, subcategories,
        TAXONOMY_VERSION, CLASSIFICATION_VERSION, tuple(evidence),
        'classified' if evidence else 'unclassified', 'unreviewed', 'not_assessed')
