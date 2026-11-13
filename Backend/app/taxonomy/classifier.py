"""Evidence-backed title rules and contextual classification results."""
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Literal

from app.taxonomy.registry import TAXONOMY_VERSION, resolve

CLASSIFICATION_VERSION = 'contextual-classifier-v1.0'
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
    Rule('television_qualifier', '1', 'entertainment', 'entertainment.television', r'\((?:television|tv) (?:series|program|show)\)\s*$', 'Explicit television program qualifier', priority=100),
    Rule('music_qualifier', '1', 'entertainment', 'entertainment.music', r'\((?:song|album|music work)\)\s*$', 'Explicit music-work qualifier', priority=100),
    Rule('video_game_qualifier', '1', 'gaming', 'gaming.video_games', r'\((?:video game|game)\)\s*$', 'Explicit video-game qualifier', priority=100),
    Rule('election', '1', 'news_politics', 'news_politics.elections', r'\b(?:presidential|general|mayoral|parliamentary|senate) elections?\b', 'Explicit election phrase', exclude_media_title=True),
    Rule('election_2026', '1', 'news_politics', 'news_politics.elections', r'\b(?:\d{4} )?(?:united states )?(?:senate )?elections?\b', 'Election wording in title', exclude_media_title=True),
    Rule('historical_event', '1', 'events', 'events.historical', r'\b(?:world war|armistice|crisis|disappearance of|killing of|deaths in|wreck of)\b', 'Historical event phrase', exclude_media_title=True),
    Rule('commemoration', '1', 'events', 'events.festivals', r'\b(?:veterans day|armistice day|memorial day)\b', 'Named commemoration', exclude_media_title=True),
    Rule('science_chemical', '1', 'science_nature', 'science_nature.chemistry', r'\b(?:chemical compound|chemical formula)\b', 'Explicit chemical context'),
    Rule('software_platform', '1', 'technology', 'technology.software', r'\b(?:software platform|digital platform)\b', 'Explicit software or digital platform wording', exclude_media_title=True),
)
VI_RULES = (
    Rule('election_vi', '1', 'news_politics', 'news_politics.elections', r'\b(?:bầu cử|tổng tuyển cử)\b', 'Vietnamese election wording', exclude_media_title=True),
    Rule('film_vi', '1', 'entertainment', 'entertainment.film', r'\((?:phim|phim truyện)(?: năm \d{4})?\)\s*$', 'Vietnamese film qualifier', priority=100),
    Rule('historical_event_vi', '1', 'events', 'events.historical', r'\b(?:chiến tranh thế giới|khủng hoảng|mất tích|vụ sát hại)\b', 'Vietnamese historical event wording', exclude_media_title=True),
)
DESCRIPTION_RULES = (
    ("business_leader_description", r"\b(?:business executive|businessperson|businessman|businesswoman|chief executive|chief technology officer|founder|entrepreneur)\b", "celebrity_public_figures.business_leaders"),
    ("entrepreneur_description", r"\bentrepreneur\b", "celebrity_public_figures.entrepreneurs"),
    ("athlete_description", r"\b(?:athlete|football player|basketball player|baseball player|professional golfer|professional boxer)\b", "celebrity_public_figures.athletes"),
    ("coach_description", r"\b(?:coach|manager of the .* team)\b", "celebrity_public_figures.coaches"),
    ("comedian_description", r"\b(?:comedian|stand-up comic)\b", "celebrity_public_figures.comedians"),
    ("actor_description", r"\b(?:actor|actress|film star)\b", "celebrity_public_figures.performers"),
    ("musician_description", r"\b(?:musician|singer|songwriter)\b", "celebrity_public_figures.musicians"),
    ("content_creator_description", r"\b(?:content creator|digital creator|youtuber|streamer)\b", "celebrity_public_figures.creators"),
    ("scientist_description", r"\b(?:scientist|researcher)\b", "celebrity_public_figures.scientists"),
    ("technology_professional_description", r"\b(?:technology (?:company|corporation|executive)|software engineer|computer scientist|technology entrepreneur|digital platform)\b", "technology"),
    ("organization_description", r"\b(?:company|corporation|nonprofit organization|government agency|sports organization)\b", "organizations"),
    ("brand_description", r"\b(?:brand|fashion label|cosmetics brand|food brand)\b", "products_brands"),
    ("chemical_description", r"\bchemical compound\b", "science_nature.chemistry"),
    ("species_description", r"\b(?:species|biological species)\b", "science_nature.biology"),
    ("ship_description", r"\b(?:ship|vessel|freighter)\b", "places_transportation.ships"),
    ("aircraft_description", r"\b(?:aircraft|airplane|aircraft type)\b", "places_transportation.aircraft"),
    ("country_description", r"\bcountry\b", "places_transportation.countries"),
    ("city_description", r"\bcity\b", "places_transportation.cities"),
    ("place_description", r"\bgeographic location\b", "places_transportation.geographic_locations"),
    ("software_description", r"\b(?:software product|software platform|operating system|web browser)\b", "technology.software"),
    ("television_description", r"\b(?:television series|tv series|television program|television show|reality television series)\b", "entertainment.television"),
    ("film_description", r"\b(?:feature film|motion picture|upcoming film|american superhero film)\b", "entertainment.film"),
    ("visual_art_description", r"\b(?:painting|artwork|work of art|oil painting|artistic work)\b", "arts_culture.visual_arts"),
    ("ship_description", r"\b(?:ocean liner|passenger ship|warship|naval vessel|steamship)\b", "places_transportation.ships"),
    ("athlete_description", r"\b(?:athlete|professional (?:boxer|wrestler|footballer|basketball player)|mixed martial artist|paralympic athlete|track and field athlete)\b", "celebrity_public_figures.athletes"),
    ("sports_event_description", r"\b(?:sports competition|sporting event|championship)\b", "sports.competitions"),
    ("place_description", r"\b(?:unincorporated community|census-designated place|village|town|city|neighborhood|geographic location)\b", "places_transportation.geographic_locations"),
    ("road_description", r"\b(?:state highway|u\.s\. route|national highway|highway|road in the united states)\b", "places_transportation.roads"),
    ("public_person_description", r"\b(?:politician|author|writer|actress|actor|television personality|internet personality|content creator|comedian|musician|singer|scientist|psychologist|professor)\b", "celebrity_public_figures.public_figures"),
)
_MEDIA = re.compile(r'\((?:\d{4}\s+)?(?:film|song|album|television series|television program|television show|tv series|tv program|tv show|video game|game)\)\s*$', re.IGNORECASE)
_COMPILED = tuple((rule, re.compile(rule.pattern, re.IGNORECASE)) for rule in RULES)


def classify(keyword_id: str, title: str, *, source: str, project: str) -> ClassificationResult:
    """English-title rules only on en.wikipedia; unknown context stays unclassified."""
    if not all(isinstance(x, str) and x.strip() for x in (keyword_id, title, source, project)):
        raise ValueError('keyword_id, title, source and project are required')
    title = unicodedata.normalize('NFC', title)
    evidence = []
    # Explicit source/project gating avoids applying English rules to arbitrary providers.
    if source == 'wikimedia_pageviews_top_per_country' and project.endswith('.wikipedia'):
        media = bool(_MEDIA.search(title))
        language = project.split('.', 1)[0]
        language_rules = _COMPILED if language == 'en' else tuple(
            (rule, re.compile(rule.pattern, re.IGNORECASE)) for rule in VI_RULES
        ) if language == 'vi' else ()
        for rule, pattern in language_rules:
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


def classify_contextual(record: dict, context: dict | None = None) -> dict:
    """Combine cached entity evidence with conservative title rules.

    Similarity scores are deliberately excluded: they may help Collection assignment,
    but do not verify entity identity or occupational classification.
    """
    from app.collections.entity_context import (
        ENTITY_TYPE_CATEGORIES,
        INSTANCE_TYPE_CATEGORIES,
        INSTANCE_TYPES,
        OCCUPATION_CATEGORIES,
    )

    context = context or {}
    keyword_id = record["id"]
    title = record.get("keyword", "")
    source = record.get("source", "") or "unknown"
    project = record.get("project", "") or "unknown"
    try:
        stored_evidence = json.loads(record.get("evidence") or "{}")
    except (TypeError, ValueError):
        stored_evidence = {}
    if not isinstance(stored_evidence, dict):
        stored_evidence = {}
    entity_type = context.get("entity_type") or "unknown"
    categories = set(context.get("category_ids") or ())
    occupations = tuple(context.get("occupations") or ())
    instance_types = tuple(context.get("instance_types") or ())
    for occupation in occupations:
        categories.update(OCCUPATION_CATEGORIES.get(occupation, ()))
    for instance_type in instance_types:
        typed = INSTANCE_TYPES.get(instance_type)
        if typed:
            category = ENTITY_TYPE_CATEGORIES.get(typed)
            if category:
                categories.add(category)
        categories.update(INSTANCE_TYPE_CATEGORIES.get(instance_type, ()))
    # Some contexts predate instance_types. Keep using their persisted high-level type.
    entity_category = ENTITY_TYPE_CATEGORIES.get(entity_type)
    if entity_category:
        categories.add(entity_category)
    rule_result = classify(keyword_id, title, source=source, project=project) if title else None
    categories.update(category for category in (rule_result.topic_categories if rule_result else ()) if category != "unclassified")
    categories.update(rule_result.subcategories if rule_result else ())
    description = context.get("description", "")
    description_evidence = []
    # A resolved Wikipedia description remains useful evidence when the page has no Wikidata QID.
    # Requiring entity_id here silently discarded this evidence and left identifiable pages UNKNOWN.
    if isinstance(description, str) and description.strip():
        for rule_id, expression, category_id in DESCRIPTION_RULES:
            match = re.search(expression, description, re.IGNORECASE)
            if match:
                resolve(category_id)
                categories.add(category_id)
                description_evidence.append({"kind": "ENTITY_DESCRIPTION_RULE", "rule_id": rule_id,
                    "matched_text": match.group(), "category_id": category_id,
                    "source": context.get("source_url", "")})
        if entity_type == "unknown" and description_evidence:
            evidence_categories = {item["category_id"] for item in description_evidence}
            if "organizations" in evidence_categories:
                entity_type = "organization"
            elif "products_brands" in evidence_categories:
                entity_type = "brand"
            elif "technology.software" in evidence_categories:
                entity_type = "software_product"
    if "celebrity_public_figures.politicians" in categories:
        categories.discard("news_politics.public_figures")
    if entity_type == "unknown" and rule_result and rule_result.rule_evidence:
        first_category = rule_result.rule_evidence[0].subcategory_id or rule_result.rule_evidence[0].category_id
        entity_type = {
            "entertainment.film": "film", "entertainment.television": "television_program",
            "entertainment.music": "music_work", "gaming.video_games": "video_game",
            "news_politics.elections": "political_event", "events.historical": "event",
            "events.festivals": "event", "technology.software": "software_product",
        }.get(first_category, entity_type)
    if entity_type == "unknown" and description_evidence:
        category_types = {
            "celebrity_public_figures.performers": "person", "celebrity_public_figures.athletes": "person",
            "celebrity_public_figures.business_leaders": "person", "celebrity_public_figures.entrepreneurs": "person",
            "celebrity_public_figures.coaches": "person", "celebrity_public_figures.comedians": "person",
            "celebrity_public_figures.musicians": "person", "celebrity_public_figures.creators": "person",
            "celebrity_public_figures.scientists": "person", "organizations": "organization",
            "products_brands": "brand", "science_nature.chemistry": "chemical_compound",
            "science_nature.biology": "biological_species", "places_transportation.countries": "country",
            "places_transportation.cities": "city", "places_transportation.ships": "ship",
            "places_transportation.aircraft": "aircraft", "places_transportation.roads": "road",
        }
        entity_type = next((category_types[item["category_id"]] for item in description_evidence
                            if item["category_id"] in category_types), entity_type)
    for category in categories:
        resolve(category)
    # Keep the most specific labels; parent taxonomy nodes remain navigable filters.
    categories = {category for category in categories if not any(
        resolve(other).parent_id == category for other in categories if other != category
    )}
    if record.get("eligible") is False:
        status, confidence, review = "INELIGIBLE", "NONE", "NOT_APPLICABLE"
    elif not categories:
        if entity_type != "unknown":
            status, confidence, review = "AMBIGUOUS", "LOW", "NEEDS_REVIEW"
        else:
            status, confidence, review = "UNKNOWN", "NONE", "NEEDS_REVIEW"
    elif len(categories) > 1 and not occupations and not instance_types and not (rule_result and rule_result.rule_evidence) and not description_evidence:
        status, confidence, review = "AMBIGUOUS", "LOW", "NEEDS_REVIEW"
    else:
        status = "CLASSIFIED"
        confidence = "HIGH" if context.get("entity_id") and (occupations or instance_types) else "MEDIUM"
        review = "UNREVIEWED"
    evidence = [asdict(item) for item in rule_result.rule_evidence] if rule_result else []
    evidence.extend(description_evidence)
    if context.get("entity_id"):
        evidence.append({"kind": "WIKIDATA_ENTITY", "entity_id": context["entity_id"],
                         "instance_types": list(instance_types), "occupations": list(occupations),
                         "description": context.get("description", "")})
    if not evidence and categories:
        evidence.append({"kind": "CACHED_ENTITY_CONTEXT", "method": context.get("method", ""),
                         "status": context.get("status", "")})
    return {
        "keyword_id": keyword_id,
        "entity_type": entity_type,
        "entity_id": context.get("entity_id"),
        "entity_description": context.get("description", ""),
        "topic_category_ids": sorted(categories),
        "subcategory_ids": sorted(category for category in categories if resolve(category).parent_id),
        "classification_status": status,
        "confidence_level": confidence,
        "commercial_relevance": "NOT_ASSESSED",
        "classification_method": "WIKIDATA_P31_P106_AND_CONTEXT_RULES" if context.get("entity_id") else (
            "WIKIPEDIA_DESCRIPTION_RULES" if description_evidence else
            "CONTEXT_TITLE_RULES" if rule_result and rule_result.rule_evidence else "INSUFFICIENT_EVIDENCE"),
        "classification_version": CLASSIFICATION_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "rule_evidence": evidence,
        "source_evidence": {"source": source, "project": project,
                            "observed_at": record.get("observed_at"),
                            "scope": {key: stored_evidence.get(key, record.get(key)) for key in (
                                "country_scope", "access", "analysis_date", "observation_date", "data_mode",
                                "provider_rank", "views_ceil", "source_quality"
                            ) if stored_evidence.get(key, record.get(key)) is not None},
                            "source_url": context.get("source_url", "")},
        "review_status": review,
        "classified_at": None,
    }
