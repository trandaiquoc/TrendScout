"""Bounded, cached Wikimedia context for conservative named-entity classification."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

CONTEXT_VERSION = "wikimedia-entity-context-v4"
MAX_TITLES_PER_REQUEST = 10
MAX_TITLE_LENGTH = 256
MAX_RESPONSE_BYTES = 4_000_000
USER_AGENT = "TrendScout/0.1 (https://github.com/trandaiquoc/TrendScout)"
_logger = logging.getLogger(__name__)

# Wikidata occupation identifiers are evidence values, not a name-to-category list.
OCCUPATION_CATEGORIES = {
    "Q33999": ("celebrity_public_figures.performers",),       # actor
    "Q82955": ("celebrity_public_figures.politicians",),
    "Q33231": ("celebrity_public_figures.photographers", "arts_culture.photography"),
    "Q40348": ("celebrity_public_figures.legal_professionals",),
    "Q39631": ("celebrity_public_figures.health_professionals", "health_fitness.health"),
    "Q177220": ("celebrity_public_figures.musicians",),       # singer
    "Q639669": ("celebrity_public_figures.musicians",),       # musician
    "Q2066131": ("celebrity_public_figures.athletes",),       # athlete
    "Q11338576": ("celebrity_public_figures.athletes",),     # boxer
    "Q10860242": ("celebrity_public_figures.athletes",),     # amateur boxer
    "Q11607585": ("celebrity_public_figures.athletes",),     # mixed martial artist
    "Q4009406": ("celebrity_public_figures.athletes",),      # track and field athlete
    "Q901": ("celebrity_public_figures.scientists",),         # scientist
    "Q41583": ("celebrity_public_figures.coaches",),           # coach
    "Q43845": ("celebrity_public_figures.business_leaders",),
    "Q131524": ("celebrity_public_figures.entrepreneurs",),
    "Q245068": ("celebrity_public_figures.comedians",),
    "Q49757": ("celebrity_public_figures.writers", "arts_culture.literature"),
    "Q483501": ("arts_culture.visual_arts",),                 # artist
    "Q5716684": ("arts_culture.performing_arts",),            # dancer
    "Q1930187": ("celebrity_public_figures.journalists",),
    "Q1650915": ("celebrity_public_figures.scientists",),      # researcher
    "Q109459317": ("celebrity_public_figures.creators",),       # content creator
    "Q111263847": ("celebrity_public_figures.creators",),       # digital creator
    "Q2906862": ("celebrity_public_figures.creators",),       # influencer
    "Q15077007": ("celebrity_public_figures.creators",),      # podcaster
    "Q1388151": ("celebrity_public_figures.political_advisers",),  # press secretary
    "Q8125919": ("celebrity_public_figures.political_advisers",),  # political adviser
}

INSTANCE_TYPES = {
    "Q5": "person",
    "Q11424": "film",
    "Q5398426": "television_program",
    "Q15416": "television_program",
    "Q7889": "video_game",
    "Q7366": "music_work",
    "Q482994": "music_work",
    "Q40231": "political_event",
    "Q16510064": "sports_event",
    "Q43229": "organization",
    "Q783794": "organization",
    "Q6881511": "organization",
    "Q163740": "organization",
    "Q327333": "organization",
    "Q12973014": "organization",
    "Q431289": "brand",
    "Q2424752": "product",
    "Q16686448": "software_product",
    "Q7397": "software_product",
    "Q4830453": "organization",
    "Q1656682": "event",
    "Q6256": "country",
    "Q515": "city",
    "Q2221906": "geographic_location",
    "Q34442": "road",
    "Q11446": "ship",
    "Q11436": "aircraft",
    "Q11173": "chemical_compound",
    "Q7432": "biological_species",
    "Q729": "animal",
    "Q756": "plant",
    "Q17537576": "scientific_concept",
    "Q571": "book",
    "Q3305213": "artwork",
    "Q17334923": "artwork",
    "Q3947": "settlement",
    "Q22698": "monument",
}

ENTITY_TYPE_CATEGORIES = {
    "film": "entertainment.film",
    "television_program": "entertainment.television",
    "video_game": "gaming.video_games",
    "music_work": "entertainment.music",
    "sports_event": "sports.competitions",
    "organization": "organizations",
    "brand": "products_brands",
    "product": "products_brands.consumer_products",
    "country": "places_transportation.countries",
    "city": "places_transportation.cities",
    "geographic_location": "places_transportation.geographic_locations",
    "road": "places_transportation.roads",
    "ship": "places_transportation.ships",
    "aircraft": "places_transportation.aircraft",
    "chemical_compound": "science_nature.chemistry",
    "biological_species": "science_nature.biology",
    "animal": "science_nature.animals",
    "plant": "science_nature.plants",
    "scientific_concept": "science_nature.concepts",
    "book": "creative_works.books",
    "artwork": "creative_works.artwork",
    "settlement": "places_transportation.settlements",
    "monument": "places_transportation.monuments_memorials",
    "digital_platform": "technology.internet_services",
}

INSTANCE_TYPE_CATEGORIES = {
    "Q4830453": ("business_finance.companies",),
    "Q327333": ("organizations.government",),
    "Q163740": ("organizations.nonprofits",),
    "Q12973014": ("organizations.sports",),
    "Q2424752": ("products_brands.consumer_products",),
    "Q16686448": ("technology.software",),
    "Q7397": ("technology.software",),
}


def _normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("_", " "))
    return " ".join(normalized.casefold().split())


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EntityContext:
    keyword_id: str
    title: str
    page_id: int | None
    entity_id: str | None
    description: str
    entity_type: str
    occupations: tuple[str, ...]
    category_ids: tuple[str, ...]
    source_url: str
    method: str
    status: str
    captured_at: str
    instance_types: tuple[str, ...] = ()
    page_namespace: int | None = None
    is_disambiguation: bool = False
    redirected_from: str = ""
    page_language: str = ""
    is_main_page: bool | None = None
    is_main_page_known: bool = False

    def as_row(self) -> tuple:
        return (
            self.keyword_id, self.title, self.page_id, self.entity_id, self.description,
            self.page_namespace, int(self.is_disambiguation), self.redirected_from, self.page_language,
            int(self.is_main_page is True), int(self.is_main_page_known),
            self.entity_type, json.dumps(self.occupations), json.dumps(self.category_ids),
            json.dumps(self.instance_types), self.source_url, self.method, self.status, CONTEXT_VERSION, self.captured_at,
        )


class WikimediaEntityContextResolver:
    """Look up only uncached Wikipedia article titles in sequential bounded batches.

    The first request binds a title to its Wikipedia page and Wikidata QID. The second
    reads descriptions, instance-of and occupation claims. Provider failures leave the
    row unknown and do not fail the user's keyword processing.
    """

    def __init__(self, store, *, transport=None, user_agent: str = USER_AGENT, timeout: float = 5.0):
        self.store = store
        self.transport = transport
        self.user_agent = user_agent
        self.timeout = httpx.Timeout(timeout, connect=min(timeout, 3.0))
        self.last_stats = {"requested_titles": 0, "resolved_titles": 0, "failed_batches": 0}

    # Exact project IDs observed in the source ledger and explicit official MediaWiki
    # hosts. Never construct a hostname from an untrusted project string.
    PROJECT_APIS = {
        "en.wikipedia": ("https://en.wikipedia.org/w/api.php", "en"),
        "fr.wikipedia": ("https://fr.wikipedia.org/w/api.php", "fr"),
        "vi.wikipedia": ("https://vi.wikipedia.org/w/api.php", "vi"),
        "id.wiktionary": ("https://id.wiktionary.org/w/api.php", "id"),
        "foundation.wikimedia": ("https://foundation.wikimedia.org/w/api.php", "en"),
        "mediawiki": ("https://www.mediawiki.org/w/api.php", "en"),
        "thankyou.wikipedia": ("https://thankyou.wikipedia.org/w/api.php", "en"),
    }

    def enrich(self, records: list[dict]) -> dict[str, EntityContext]:
        uncached = self._uncached_records(records)
        if not uncached:
            self.last_stats = {"requested_titles": 0, "resolved_titles": 0, "failed_batches": 0}
            return self.cached(record["id"] for record in records)
        results: dict[str, EntityContext] = {}
        stats = {"requested_titles": len(uncached), "resolved_titles": 0, "failed_batches": 0}
        with httpx.Client(timeout=self.timeout, transport=self.transport,
                          headers={"User-Agent": self.user_agent}, follow_redirects=False) as client:
            for project, (api, language, group) in self._groups(uncached).items():
                for offset in range(0, len(group), MAX_TITLES_PER_REQUEST):
                    batch = group[offset:offset + MAX_TITLES_PER_REQUEST]
                    try:
                        resolved = self._resolve_batch(client, api, language, batch)
                        results.update(resolved)
                        stats["resolved_titles"] += len(resolved)
                    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                        stats["failed_batches"] += 1
                        _logger.warning("Wikimedia entity context batch failed",
                                        extra={"project": project, "batch_size": len(batch),
                                               "error_type": type(exc).__name__})
        self.last_stats = stats
        self._persist(results)
        return self.cached(record["id"] for record in records)

    def cached(self, keyword_ids):
        ids = list(dict.fromkeys(keyword_ids))
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with self.store.connect() as db:
            rows = db.execute(f"SELECT * FROM entity_context WHERE keyword_id IN ({marks})", ids).fetchall()
        found = {}
        for row in rows:
            found[row["keyword_id"]] = EntityContext(
                row["keyword_id"], row["title"], row["page_id"], row["entity_id"],
                row["description"], row["entity_type"], tuple(json.loads(row["occupations"])),
                tuple(json.loads(row["category_ids"])), row["source_url"], row["method"],
                row["status"], row["captured_at"], tuple(json.loads(row["instance_types"] or "[]")),
                row["page_namespace"], bool(row["is_disambiguation"]), row["redirected_from"],
                row["page_language"],
                bool(row["is_main_page"]) if bool(row["is_main_page_known"]) else None,
                bool(row["is_main_page_known"]),
            )
        return found

    def _uncached_records(self, records):
        ids = list(dict.fromkeys(row["id"] for row in records))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self.store.connect() as db:
            cached_ids = {row[0] for row in db.execute(
                f"SELECT keyword_id FROM entity_context WHERE context_version=? AND keyword_id IN ({marks})",
                (CONTEXT_VERSION, *ids),
            )}
        return [row for row in records if row["id"] not in cached_ids
                and row.get("source") == "wikimedia_pageviews_top_per_country"
                and row.get("project") in WikimediaEntityContextResolver.PROJECT_APIS
                and isinstance(row.get("keyword"), str)
                and "|" not in row["keyword"]
                and 1 <= len(row["keyword"]) <= MAX_TITLE_LENGTH]

    @staticmethod
    def _groups(records):
        groups = {}
        for row in records:
            project = row["project"]
            api, language = WikimediaEntityContextResolver.PROJECT_APIS[project]
            entry = groups.setdefault(project, (api, language, []))
            entry[2].append(row)
        return groups

    def _resolve_batch(self, client, api, language, records):
        titles = [row["keyword"] for row in records]
        page_data = self._get_json(client, api, {
            "action": "query", "format": "json", "formatversion": "2",
            "meta": "siteinfo", "siprop": "general|namespaces|namespacealiases",
            "prop": "pageprops|description|info|extracts", "ppprop": "wikibase_item|disambiguation",
            "inprop": "url", "exintro": "1", "explaintext": "1", "exchars": "1200",
            "redirects": "1", "titles": "|".join(titles),
        })
        query = page_data.get("query", {})
        pages = query.get("pages", [])
        if type(pages) is not list:
            raise ValueError("invalid Wikipedia page response")
        general = query.get("general", {})
        if not isinstance(general, dict):
            general = {}
        namespace_ids = {}
        for collection in (query.get("namespaces", {}),):
            if isinstance(collection, dict):
                for identifier, item in collection.items():
                    try:
                        namespace_ids[_normalize_title(str(item.get("name", "")))] = int(identifier)
                        namespace_ids[_normalize_title(str(item.get("canonical", "")))] = int(identifier)
                    except (AttributeError, TypeError, ValueError):
                        continue
        aliases = query.get("namespacealiases", [])
        if isinstance(aliases, list):
            for item in aliases:
                if not isinstance(item, dict) or not isinstance(item.get("alias"), str):
                    continue
                try:
                    namespace_ids[_normalize_title(item["alias"])] = int(item["id"])
                except (KeyError, TypeError, ValueError):
                    continue
        redirects = {_normalize_title(item["from"]): item["to"] for item in query.get("redirects", [])
                     if isinstance(item, dict) and isinstance(item.get("from"), str)
                     and isinstance(item.get("to"), str)}
        by_title = {_normalize_title(page.get("title", "")): page for page in pages
                    if isinstance(page, dict) and isinstance(page.get("title"), str)}
        bindings = {}
        qids = []
        for record in records:
            normalized = _normalize_title(record["keyword"])
            canonical = _normalize_title(redirects.get(normalized, record["keyword"]))
            page = by_title.get(normalized) or by_title.get(canonical)
            if not page:
                continue
            qid = page.get("pageprops", {}).get("wikibase_item")
            if isinstance(qid, str) and qid.startswith("Q") and qid[1:].isdigit():
                qids.append(qid)
            bindings[record["id"]] = page

        entities = {}
        if qids:
            entity_data = self._get_json(client, "https://www.wikidata.org/w/api.php", {
                "action": "wbgetentities", "format": "json", "formatversion": "2",
                "ids": "|".join(dict.fromkeys(qids)), "props": "claims|descriptions",
                "languages": "en|vi",
            })
            entities = entity_data.get("entities", {})
            if type(entities) is not dict:
                raise ValueError("invalid Wikidata entity response")

        output = {}
        for record in records:
            page = bindings.get(record["id"])
            qid = (page or {}).get("pageprops", {}).get("wikibase_item")
            entity = entities.get(qid, {}) if isinstance(qid, str) else {}
            description = (page or {}).get("description", "")
            if not isinstance(description, str):
                description = ""
            if not description and isinstance((page or {}).get("extract"), str):
                description = page["extract"][:1200]
            page_title = page.get("title", record["keyword"]) if isinstance(page, dict) else record["keyword"]
            page_namespace = page.get("ns") if isinstance(page, dict) else None
            if type(page_namespace) is not int and ":" in record["keyword"]:
                namespace_prefix = _normalize_title(record["keyword"].split(":", 1)[0])
                page_namespace = namespace_ids.get(namespace_prefix)
            wikidata_descriptions = entity.get("descriptions", {})
            if isinstance(wikidata_descriptions, dict):
                description = wikidata_descriptions.get("en", {}).get("value") or description
            description = description[:1200]
            instance = self._claim_ids(entity, "P31")
            occupations = tuple(self._claim_ids(entity, "P106"))
            entity_type = next((INSTANCE_TYPES[value] for value in instance if value in INSTANCE_TYPES), "unknown")
            categories = tuple(dict.fromkeys(
                category for occupation in occupations
                for category in OCCUPATION_CATEGORIES.get(occupation, ())
            ))
            type_category = ENTITY_TYPE_CATEGORIES.get(entity_type)
            if type_category:
                categories = tuple(dict.fromkeys((*categories, type_category)))
            elif entity_type == "political_event":
                title_and_description = f"{page_title} {description}".casefold()
                political_category = ("news_politics.elections" if re.search(
                    r"\b(elections?|electoral|bầu cử)\b", title_and_description)
                    else "news_politics.current_events")
                categories = tuple(dict.fromkeys((*categories, political_category)))
            page_id = page.get("pageid") if isinstance(page, dict) else None
            source_url = (page or {}).get("fullurl") or (
                api.rsplit("/w/api.php", 1)[0] + "/wiki/" + quote(page_title.replace(" ", "_"))
            )
            status = "CLASSIFIED" if entity_type != "unknown" or categories else "UNKNOWN"
            method = "WIKIPEDIA_PAGE_ID_AND_WIKIDATA_CLAIMS" if qid else "WIKIPEDIA_PAGE_WITHOUT_QID"
            output[record["id"]] = EntityContext(
                record["id"], page_title, page_id if type(page_id) is int else None,
                qid if isinstance(qid, str) else None, description, entity_type,
                occupations, categories, source_url, method, status, _timestamp(), instance,
                page_namespace if type(page_namespace) is int else None,
                "disambiguation" in ((page or {}).get("pageprops") or {}),
                next((source_title for source_title, target_title in redirects.items()
                      if _normalize_title(target_title) == _normalize_title(page_title)), ""),
                (page or {}).get("pagelanguage")
                if isinstance((page or {}).get("pagelanguage"), str) else language,
                (_normalize_title(general["mainpage"]) == _normalize_title(page_title)
                 if isinstance(general.get("mainpage"), str) and general["mainpage"].strip() else None),
                isinstance(general.get("mainpage"), str) and bool(general["mainpage"].strip()),
            )
        return output

    @staticmethod
    def _claim_ids(entity, property_id):
        claims = entity.get("claims", {}) if isinstance(entity, dict) else {}
        statements = claims.get(property_id, []) if isinstance(claims, dict) else []
        result = []
        for statement in statements if isinstance(statements, list) else []:
            value = statement.get("mainsnak", {}).get("datavalue", {}).get("value", {})
            identifier = value.get("id") if isinstance(value, dict) else None
            if isinstance(identifier, str) and identifier.startswith("Q") and identifier[1:].isdigit():
                result.append(identifier)
        return list(dict.fromkeys(result))

    @staticmethod
    def _get_json(client, url, params):
        response = client.get(url, params=params)
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ValueError("context response exceeds size limit")
        value = response.json()
        if not isinstance(value, dict) or "error" in value:
            raise ValueError("context provider returned an invalid response")
        return value

    def _persist(self, results):
        if not results:
            return
        with self.store.connect() as db:
            db.executemany("""INSERT INTO entity_context(keyword_id,title,page_id,entity_id,description,
                page_namespace,is_disambiguation,redirected_from,page_language,is_main_page,is_main_page_known,entity_type,occupations,
                category_ids,instance_types,source_url,method,status,context_version,captured_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(keyword_id) DO UPDATE SET
                title=excluded.title,page_id=excluded.page_id,entity_id=excluded.entity_id,
                description=excluded.description,page_namespace=excluded.page_namespace,
                is_disambiguation=excluded.is_disambiguation,redirected_from=excluded.redirected_from,
                page_language=excluded.page_language,is_main_page=excluded.is_main_page,
                is_main_page_known=excluded.is_main_page_known,entity_type=excluded.entity_type,
                occupations=excluded.occupations,category_ids=excluded.category_ids,instance_types=excluded.instance_types,
                source_url=excluded.source_url,method=excluded.method,status=excluded.status,
                context_version=excluded.context_version,captured_at=excluded.captured_at""",
                [value.as_row() for value in results.values()])
