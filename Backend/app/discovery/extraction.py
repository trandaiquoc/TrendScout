"""Deterministic candidate extraction from authorized observations, not query seeds."""

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.discovery.contracts import Candidate, Evidence, Observation, ProcessingGrant
from app.sources.catalog import source_capability
from app.sources.serpapi.normalization import canonical_url
from app.taxonomy.registry import TAXONOMY_VERSION

EXTRACTION_VERSION = "phrase-rules-v1"
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_STOP = frozenset("a an and are as at be by for from in is it of on or the to was with".split())
_ALIASES = {"tee shirt": "t shirt"}  # Explicit synonym only; never fuzzy-merge entities.
_TOPICS = {
    "fashion": frozenset("fashion clothing clothes shirt shirts pants dress dresses skirt shoes footwear bags handbag accessories linen".split()),
    "home_living": frozenset("home kitchen sink organizer organisation organization storage decor furniture appliance household".split()),
    "technology": frozenset("technology computer software ai robot robotics electronics gadget gadgets glasses".split()),
    "food_beverage": frozenset("food beverage cooking coffee tea recipe recipes".split()),
    "gaming": frozenset("game games gaming esports".split()),
    "entertainment": frozenset("entertainment film movie movies music television tv anime cinema".split()),
    "celebrity_public_figures": frozenset("celebrity celebrities actor actors musician musicians creator creators influencer influencers".split()),
    "beauty_personal_care": frozenset("beauty skincare cosmetics makeup haircare shampoo perfume".split()),
    "sports": frozenset("sport sports athlete athletes football basketball baseball soccer tennis".split()),
    "health_fitness": frozenset("health fitness wellness workout exercise yoga".split()),
    "travel_lifestyle": frozenset("travel tourism vacation destination destinations lifestyle".split()),
    "business_finance": frozenset("business finance financial economy company companies stocks markets".split()),
    "news_politics": frozenset("news politics political election elections government policy".split()),
}
_MAX_TEXT = 10_000
_MAX_TOKENS = 100
_MAX_OBSERVATIONS = 10_000


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC-aware timestamps are required")
    return value.astimezone(timezone.utc)


def _validate(observation: Observation, grants: dict[str, ProcessingGrant], now: datetime) -> None:
    if not isinstance(observation, Observation):
        raise ValueError("Observation contract required")
    capability = source_capability(observation.source_id)
    if capability.provider_id != observation.provider_id:
        raise ValueError("Provider does not match source")
    if observation.data_mode not in {"LIVE", "SYNTHETIC"}:
        raise ValueError("Unsupported data mode")
    if observation.delivery_mode not in {"FRESH", "CACHED"}:
        raise ValueError("Unsupported delivery mode")
    if observation.market_evidence not in {"US_COUNTRY_SCOPED", "US_FILTER_ONLY", "UNKNOWN"}:
        raise ValueError("Unsupported market evidence")
    if (not all(isinstance(x, str) and x.strip() and len(x) <= 256 for x in (
            observation.document_id, observation.scope, observation.parser_version))
            or not isinstance(observation.text, str) or not observation.text.strip()
            or len(observation.text) > _MAX_TEXT or "\x00" in observation.text):
        raise ValueError("Invalid observation fields")
    try:
        observation.text.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("Invalid observation encoding") from None
    if (not isinstance(observation.query_evidence, tuple)
            or len(observation.query_evidence) > 100
            or any(not isinstance(q, str) or not q or len(q) > 256 for q in observation.query_evidence)):
        raise ValueError("Invalid query evidence")
    observed, fetched = _utc(observation.observed_at), _utc(observation.fetched_at)
    if observed > fetched or fetched > now:
        raise ValueError("Observation or fetch time is in the future")
    if observation.language != "en":
        raise ValueError("Only explicitly identified English text is supported")
    if observation.source_url is not None:
        try:
            parsed = urlsplit(observation.source_url)
            if (len(observation.source_url) > 2048 or parsed.scheme != "https"
                    or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.port is not None):
                raise ValueError("Invalid evidence URL")
            canonical_url(observation.source_url)
        except ValueError:
            raise ValueError("Invalid evidence URL") from None
    if observation.data_mode == "LIVE":
        grant = grants.get(observation.source_id)
        if (not isinstance(grant, ProcessingGrant) or grant.source_id != observation.source_id
                or grant.extraction_allowed is not True or not grant.policy_version.strip()
                or _utc(grant.expires_at) <= now):
            raise PermissionError("Source extraction approval is unavailable")


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _phrases(text: str) -> tuple[tuple[str, str], ...]:
    # Reject truncated input instead of quietly claiming full extraction coverage.
    words = _WORD.findall(unicodedata.normalize("NFKC", text))
    if len(words) > _MAX_TOKENS:
        raise ValueError("Observation exceeds token limit")
    normalized = [word.casefold().replace("’", "'") for word in words]
    candidates = []
    for width in range(1, 5):
        for start in range(len(words) - width + 1):
            part = normalized[start:start + width]
            if part[0] in _STOP or part[-1] in _STOP:
                continue
            if width == 1 and (len(part[0]) < 2 or part[0].isdigit()):
                continue
            if all(token.isdigit() for token in part):
                continue
            phrase = " ".join(part)
            candidates.append((_ALIASES.get(phrase, phrase), " ".join(words[start:start + width])))
    return tuple(dict.fromkeys(candidates))


def _categories(keyword: str) -> tuple[str, ...]:
    words = frozenset(_WORD.findall(keyword))
    return tuple(name for name, vocabulary in _TOPICS.items() if words & vocabulary) or (
        "unclassified",
    )


def extract_candidates(observations: tuple[Observation, ...], *,
                       grants: tuple[ProcessingGrant, ...] = (),
                       now: datetime) -> tuple[Candidate, ...]:
    """Extract all bounded candidates and retain duplicate/query relationships.

    Source content is treated as data; this function never executes it or calls a provider.
    """
    now = _utc(now)
    if len(observations) > _MAX_OBSERVATIONS:
        raise ValueError("Observation batch exceeds limit")
    by_source = {grant.source_id: grant for grant in grants}
    if len(by_source) != len(grants):
        raise ValueError("Duplicate source processing grant")
    records = defaultdict(list)
    forms = defaultdict(set)
    doc_content = {}
    for observation in observations:
        _validate(observation, by_source, now)
        content = _normalize(observation.text)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        document_key = (observation.source_id, observation.document_id, observation.data_mode,
                        _utc(observation.observed_at))
        if document_key in doc_content and doc_content[document_key] != content_hash:
            raise ValueError("A document ID has conflicting content in the batch")
        doc_content[document_key] = content_hash
        evidence = Evidence(
            observation.source_id, observation.provider_id, observation.document_id, content_hash,
            _utc(observation.observed_at), _utc(observation.fetched_at), observation.scope,
            observation.query_evidence, observation.market_evidence, observation.data_mode,
            observation.parser_version,
            canonical_url(observation.source_url) if observation.source_url else None,
            observation.delivery_mode,
        )
        for phrase, original in _phrases(observation.text):
            forms[phrase].add(original)
            if evidence not in records[phrase]:
                records[phrase].append(evidence)
    output = []
    for phrase in sorted(records):
        identifier = hashlib.sha256(json.dumps(
            [EXTRACTION_VERSION, phrase], ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()
        categories = _categories(phrase)
        flags = ("RULE_BASED_UNVALIDATED", "COVERAGE_LIMITED")
        if categories == ("unclassified",):
            flags += ("UNCLASSIFIED",)
        evidence = tuple(sorted(records[phrase], key=lambda x: (
            x.source_id, x.scope, x.observed_at, x.document_id, x.query_evidence
        )))
        output.append(Candidate(identifier, phrase, tuple(sorted(forms[phrase])), categories,
                                TAXONOMY_VERSION, EXTRACTION_VERSION, flags, evidence))
    return tuple(output)
