"""Compatibility classifier for the persisted demo's title-rules-v0.1."""
import re

from app.models.keywords import ClassificationEvidence
from app.taxonomy.registry import resolve

CLASSIFICATION_VERSION = 'title-rules-v0.1'
_RULES = (
    ('fashion', 'fashion_industry', r'\b(?:fashion industry|fashion design|fashion designer|fashion week|haute couture|fashion house|clothing brand|clothing company|clothing retailer|clothing manufacturer|clothing industry|wedding dress|wedding dresses|fashion retail)\b'),
    ('home_goods', 'home_products', r'\b(?:home appliance|home appliances|household appliance|household appliances|kitchen appliance|kitchen appliances|vacuum cleaner|vacuum cleaners|washing machine|washing machines|dishwasher|dishwashers|air fryer|air fryers|cookware|furniture manufacturer|furniture retailer|home furnishings)\b'),
    ('other', 'elections', r'\b(?:presidential election|general election|mayoral election|parliamentary election|senate election)\b'),
    ('other', 'film_or_television', r'\((?:\d{4}\s+)?(?:film|television series|tv series|tv show)\)\s*$'),
    ('other', 'professional_sport', r'\b(?:national football league|major league baseball|national basketball association|world series)\b'),
)
_COMPILED = tuple((cat, rule, re.compile(pattern, re.IGNORECASE)) for cat, rule, pattern in _RULES)
_MEDIA_QUALIFIER = re.compile(r'\((?:\d{4}\s+)?(?:film|song|album|television series|tv series|tv show)\)\s*$', re.IGNORECASE)


def classify_legacy(title: str) -> tuple[tuple[str, ...], tuple[ClassificationEvidence, ...]]:
    evidence = []
    media_qualified = bool(_MEDIA_QUALIFIER.search(title))
    for category, rule_id, pattern in _COMPILED:
        if category in ('fashion', 'home_goods') and media_qualified:
            continue
        for match in pattern.finditer(title):
            evidence.append(ClassificationEvidence(category, rule_id, match.group(0)))
    primary = {ev.category for ev in evidence if ev.category in ('fashion', 'home_goods')}
    categories = tuple(cat for cat in ('fashion', 'home_goods') if cat in primary)
    if not categories:
        categories = ('other',) if any(ev.category == 'other' for ev in evidence) else ('unclassified',)
    else:
        evidence = [ev for ev in evidence if ev.category in primary]
    for category in categories:
        resolve(category)  # Validate legacy labels through the versioned taxonomy.
    return categories, tuple(evidence)
