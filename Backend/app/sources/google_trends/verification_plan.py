"""Dynamic, bounded Google SERP plan derived from validated trend candidates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from app.sources.catalog import GOOGLE_SOURCE_ID, source_capability
from app.sources.google_trends.trending_now import normalize_keyword
from app.sources.serpapi.models import QueryContext

VERIFICATION_PLAN_ID = "google-trend-candidate-serp"
VERIFICATION_PLAN_VERSION = 1
MAX_VERIFICATION_QUERIES = 2
VERIFICATION_PARSER_VERSION = "google-trend-serp-v1"


@dataclass(frozen=True)
class VerificationPlan:
    plan_id: str
    version: int
    policy_fingerprint: str
    batch_started_at: datetime
    contexts: tuple[QueryContext, ...]
    candidate_ids: tuple[str, ...]
    fingerprint: str


def _scope(value: object) -> str:
    if not isinstance(value, (list, tuple)):
        return "open_discovery"
    values = [item.get("name") if isinstance(item, dict) else item for item in value]
    rows = sorted({" ".join(item.split()) for item in values
                   if isinstance(item, str) and item.strip() and len(item) <= 128})
    scope = ",".join(rows[:8])
    return scope or "open_discovery"


def select_verification_candidates(candidates: tuple[dict, ...], *,
                                   previously_observed: frozenset[str] = frozenset(),
                                   recently_verified: frozenset[str] = frozenset(),
                                   limit: int = MAX_VERIFICATION_QUERIES) -> tuple[dict, ...]:
    """Choose a diverse, explainable set; input terms remain server validated.

    Novel candidates lead, recent verification is excluded, and provider trend
    signals break ties. Fashion/Home & Living are a preference only: if neither
    is present, or a second category adds diversity, other topics remain eligible.
    """
    if (not isinstance(candidates, tuple) or type(limit) is not int
            or not 0 <= limit <= MAX_VERIFICATION_QUERIES
            or not isinstance(previously_observed, frozenset)
            or not isinstance(recently_verified, frozenset)):
        raise ValueError("bounded candidate selection inputs are required")
    prepared = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            _, identity = normalize_keyword(candidate.get("canonical_keyword"))
        except ValueError:
            continue
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if candidate.get("canonical_keyword_identity") != digest or digest in recently_verified:
            continue
        clusters = candidate.get("trend_clusters")
        cluster = (clusters[0] if isinstance(clusters, list) and clusters
                   and isinstance(clusters[0], dict) else {})
        categories = _scope(cluster.get("categories")).split(",")
        preferred = int(any(item.casefold() in {
            "fashion", "home", "home & garden", "home & living"
        } for item in categories))
        increase = cluster.get("increase_percentage")
        increase = (float(increase) if type(increase) in {int, float}
                    and not isinstance(increase, bool) and 0 <= increase <= 1_000_000_000 else 0.0)
        active = cluster.get("active") is True
        newness = int(digest not in previously_observed)
        prepared.append(((-newness, -active, -increase, -preferred, digest),
                         categories[0] if categories else "open_discovery",
                         bool(preferred), candidate))
    prepared.sort(key=lambda row: row[0])
    selected, seen_categories = [], set()
    # Give one slot to a Fashion/Home candidate and one to another topic whenever
    # both groups are available. Preference cannot consume the whole two-query cap.
    if limit:
        preferred = next((row for row in prepared if row[2]), None)
        general = next((row for row in prepared if not row[2]), None)
        for row in (preferred, general):
            if row is not None and len(selected) < limit:
                selected.append(row[3])
                seen_categories.add(row[1])
    # Then favor category diversity before filling by the same signal score.
    for _, category, _, candidate in prepared:
        if len(selected) >= limit:
            break
        if category in seen_categories:
            continue
        selected.append(candidate)
        seen_categories.add(category)
    if len(selected) < limit:
        chosen = {item.get("canonical_keyword_identity") for item in selected}
        for _, _, _, candidate in prepared:
            if len(selected) >= limit:
                break
            if candidate.get("canonical_keyword_identity") not in chosen:
                selected.append(candidate)
                chosen.add(candidate.get("canonical_keyword_identity"))
    return tuple(selected)


def _query_fingerprint(query: str, scope: str) -> str:
    payload = {
        "plan_id": VERIFICATION_PLAN_ID,
        "plan_version": VERIFICATION_PLAN_VERSION,
        "source_id": GOOGLE_SOURCE_ID,
        "provider_id": source_capability(GOOGLE_SOURCE_ID).provider_id,
        "query": query,
        "scope": scope,
        "market": "US",
        "language": "en",
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def build_verification_plan(candidates: tuple[dict, ...], *, policy_fingerprint: str,
                            started_at: datetime) -> VerificationPlan:
    """Bind no more than two server-selected candidates to a versioned SERP plan."""
    if (not isinstance(candidates, tuple) or len(candidates) > MAX_VERIFICATION_QUERIES
            or not isinstance(policy_fingerprint, str) or len(policy_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in policy_fingerprint)
            or not isinstance(started_at, datetime) or started_at.tzinfo is None
            or started_at.utcoffset() is None):
        raise ValueError("bounded Google verification plan inputs are required")
    capability = source_capability(GOOGLE_SOURCE_ID)
    contexts, candidate_ids, seen = [], [], set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("selected trend candidate must be a validated record")
        query, identity = normalize_keyword(candidate.get("canonical_keyword"))
        if candidate.get("canonical_keyword_identity") != hashlib.sha256(
                identity.encode("utf-8")).hexdigest():
            raise ValueError("selected trend candidate identity is invalid")
        if identity in seen:
            continue
        seen.add(identity)
        scope = _scope(candidate.get("trend_clusters", [{}])[0].get("categories")
                       if candidate.get("trend_clusters") else None)
        fingerprint = _query_fingerprint(query, scope)
        contexts.append(QueryContext(
            source_id=GOOGLE_SOURCE_ID,
            provider_id=capability.provider_id,
            plan_id=VERIFICATION_PLAN_ID,
            plan_version=VERIFICATION_PLAN_VERSION,
            plan_fingerprint=policy_fingerprint,
            query_fingerprint=fingerprint,
            canonical_query=query,
            taxonomy_scope=scope,
            started_at=started_at.astimezone(timezone.utc),
        ))
        candidate_ids.append(candidate["canonical_keyword_identity"])
    if len(contexts) > MAX_VERIFICATION_QUERIES:
        raise ValueError("verification plan exceeds its two-query budget")
    plan_fingerprint = hashlib.sha256(json.dumps(
        {"plan_id": VERIFICATION_PLAN_ID, "version": VERIFICATION_PLAN_VERSION,
         "policy_fingerprint": policy_fingerprint,
         "queries": [{"query": row.canonical_query,
                      "query_fingerprint": row.query_fingerprint}
                     for row in contexts]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return VerificationPlan(
        VERIFICATION_PLAN_ID, VERIFICATION_PLAN_VERSION, policy_fingerprint,
        started_at.astimezone(timezone.utc), tuple(contexts), tuple(candidate_ids),
        plan_fingerprint,
    )


def validate_dynamic_context(context: QueryContext, *, policy_fingerprint: str) -> bool:
    if (not isinstance(context, QueryContext)
            or context.source_id != GOOGLE_SOURCE_ID
            or context.provider_id != source_capability(GOOGLE_SOURCE_ID).provider_id
            or context.plan_id != VERIFICATION_PLAN_ID
            or context.plan_version != VERIFICATION_PLAN_VERSION
            or context.plan_fingerprint != policy_fingerprint
            or context.taxonomy_scope != _scope(context.taxonomy_scope.split(","))):
        return False
    try:
        query, identity = normalize_keyword(context.canonical_query)
    except ValueError:
        return False
    return (query == context.canonical_query
            and context.query_fingerprint == _query_fingerprint(query, context.taxonomy_scope)
            and isinstance(identity, str))
