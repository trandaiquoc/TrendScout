"""Versioned evidence policy consumed by the contextual classifier.

This is a deterministic local policy, not a generative prompt or an LLM integration.
Changing entity evidence mappings, source gates, or description rules requires a version bump.
"""

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class EntityDescriptionRule:
    rule_id: str
    expression: str
    category_id: str


VERSION = "contextual-classifier-v1.2"
TITLE_RULE_SOURCE = "wikimedia_pageviews_top_per_country"
TITLE_RULE_PROJECTS = frozenset({"en.wikipedia", "vi.wikipedia"})
VERIFIED_PAGE_HOSTS = frozenset({
    "en.wikipedia.org", "fr.wikipedia.org", "vi.wikipedia.org", "id.wiktionary.org",
    "foundation.wikimedia.org", "www.mediawiki.org", "thankyou.wikipedia.org",
})


def permits_title_rules(source: str, project: str) -> bool:
    """Keep title rules restricted to the two languages with reviewed local rules."""
    return source == TITLE_RULE_SOURCE and project in TITLE_RULE_PROJECTS


def is_verified_page_context(method: str, source_url: str) -> bool:
    if method not in {"WIKIPEDIA_PAGE_ID_AND_WIKIDATA_CLAIMS", "WIKIPEDIA_PAGE_WITHOUT_QID"}:
        return False
    try:
        parsed = urlparse(source_url)
    except (TypeError, ValueError):
        return False
    return parsed.scheme == "https" and parsed.hostname in VERIFIED_PAGE_HOSTS and parsed.path.startswith("/wiki/")


ENTITY_DESCRIPTION_RULES = (
    EntityDescriptionRule("business_leader_description", r"\b(?:business executive|businessperson|businessman|businesswoman|chief executive|chief technology officer|founder|entrepreneur)\b", "celebrity_public_figures.business_leaders"),
    EntityDescriptionRule("entrepreneur_description", r"\bentrepreneur\b", "celebrity_public_figures.entrepreneurs"),
    EntityDescriptionRule("athlete_description", r"\b(?:athlete|football player|football quarterback|basketball player|baseball player|boxer|sprinter|mixed martial artist|para athletics competitor|professional golfer)\b", "celebrity_public_figures.athletes"),
    EntityDescriptionRule("coach_description", r"\b(?:coach|manager of the .* team)\b", "celebrity_public_figures.coaches"),
    EntityDescriptionRule("comedian_description", r"\b(?:comedian|stand-up comic)\b", "celebrity_public_figures.comedians"),
    EntityDescriptionRule("actor_description", r"\b(?:actor|actress|film star|media personality)\b", "celebrity_public_figures.performers"),
    EntityDescriptionRule("musician_description", r"\b(?:musician|singer|songwriter)\b", "celebrity_public_figures.musicians"),
    EntityDescriptionRule("content_creator_description", r"\b(?:content creator|digital creator|youtuber|streamer|podcast host|influencer)\b", "celebrity_public_figures.creators"),
    EntityDescriptionRule("scientist_description", r"\b(?:scientist|researcher)\b", "celebrity_public_figures.scientists"),
    EntityDescriptionRule("health_professional_description", r"\b(?:psychologist|physician|doctor|therapist|health professional)\b", "celebrity_public_figures.health_professionals"),
    EntityDescriptionRule("writer_description", r"\bwriter\b", "celebrity_public_figures.writers"),
    EntityDescriptionRule("technology_professional_description", r"\b(?:technology (?:company|corporation|executive)|software engineer|computer scientist|technology entrepreneur|digital platform)\b", "technology"),
    EntityDescriptionRule("organization_description", r"\b(?:company|corporation|nonprofit organization|nonprofit online|government agency|sports organization)\b", "organizations"),
    EntityDescriptionRule("nonprofit_description", r"\bnonprofit\b", "organizations.nonprofits"),
    EntityDescriptionRule("media_organization_description", r"\b(?:online encyclopedia|media organization)\b", "organizations.media"),
    EntityDescriptionRule("brand_description", r"\b(?:brand|fashion label|cosmetics brand|food brand)\b", "products_brands"),
    EntityDescriptionRule("chemical_description", r"\bchemical compound\b", "science_nature.chemistry"),
    EntityDescriptionRule("biology_description", r"\b(?:species|biological species|evolution of|diversification of .* through geologic time)\b", "science_nature.biology"),
    EntityDescriptionRule("plant_description", r"\b(?:plant|tree|sequoia)\b", "science_nature.plants"),
    EntityDescriptionRule("ship_description", r"\b(?:ship|vessel|freighter|ocean liner)\b", "places_transportation.ships"),
    EntityDescriptionRule("aircraft_description", r"\b(?:aircraft|airplane|aircraft type)\b", "places_transportation.aircraft"),
    EntityDescriptionRule("country_description", r"\bcountry\b", "places_transportation.countries"),
    EntityDescriptionRule("city_description", r"\bcity\b", "places_transportation.cities"),
    EntityDescriptionRule("settlement_description", r"\b(?:township|settlement|town|village)\b", "places_transportation.settlements"),
    EntityDescriptionRule("monument_description", r"\b(?:monument|memorial|funerary monument)\b", "places_transportation.monuments_memorials"),
    EntityDescriptionRule("road_description", r"\b(?:highway|numbered route|road)\b", "places_transportation.roads"),
    EntityDescriptionRule("place_description", r"\bgeographic location\b", "places_transportation.geographic_locations"),
    EntityDescriptionRule("software_description", r"\b(?:software product|software platform|operating system|web browser|wiki software)\b", "technology.software"),
    EntityDescriptionRule("internet_service_description", r"\b(?:online (?:repository|encyclopedia|service|platform)|digital platform|website|fanfiction repository)\b", "technology.internet_services"),
    EntityDescriptionRule("api_service_description", r"\b(?:API|application programming interface|developer interface)s?\b", "technology.internet_services"),
    EntityDescriptionRule("television_description", r"\btelevision (?:program|series|show)\b", "entertainment.television"),
    EntityDescriptionRule("film_description", r"\b(?:film|movie)\b", "entertainment.film"),
    EntityDescriptionRule("artwork_description", r"\b(?:painting|artwork|sculpture)\b", "creative_works.artwork"),
    EntityDescriptionRule("crime_justice_description", r"\b(?:murderer|convicted criminal|serial killer|criminal)\b", "law_society.crime_justice"),
)
