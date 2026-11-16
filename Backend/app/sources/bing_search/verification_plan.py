"""Discovery-first Bing queries selected from persisted Google observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, source_capability
from app.sources.google_trends.trending_now import normalize_keyword
from app.sources.serpapi.models import QueryContext

PLAN_ID = "bing-saved-candidate-serp"
PLAN_VERSION = 1
MAX_BING_VERIFICATIONS = 1


@dataclass(frozen=True)
class BingCandidate:
    keyword: str
    identity: str
    source_id: str
    observed_at: datetime
    fetched_at: datetime
    reason: str


@dataclass(frozen=True)
class BingVerificationPlan:
    policy_fingerprint: str
    started_at: datetime
    candidates: tuple[BingCandidate, ...]
    contexts: tuple[QueryContext, ...]


def select_saved_candidates(rows: tuple[dict, ...], *, now: datetime,
                            recently_verified: frozenset[str] = frozenset(),
                            limit: int = MAX_BING_VERIFICATIONS) -> tuple[BingCandidate, ...]:
    """Choose one deterministic Google-sourced candidate with explicit provenance.

    Rows must be persisted Google Trends observations; broad Bing seed queries and
    caller-provided terms are rejected. Recently verified identities are skipped.
    """
    if (not isinstance(rows, tuple) or not isinstance(recently_verified, frozenset)
            or type(limit) is not int or not 0 <= limit <= MAX_BING_VERIFICATIONS
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("bounded saved-candidate inputs are required")
    now = now.astimezone(timezone.utc)
    prepared = {}
    for row in rows:
        if (not isinstance(row, dict) or row.get("source_id") != GOOGLE_SOURCE_ID
                or row.get("data_mode") != "LIVE"):
            continue
        project = row.get("project")
        if not isinstance(project, str) or not project.startswith("google_trends:"):
            continue
        try:
            keyword, identity = normalize_keyword(row.get("canonical_keyword"))
            observed = _timestamp(row.get("observed_at"))
            fetched = _timestamp(row.get("fetched_at"))
        except (TypeError, ValueError):
            continue
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if digest in recently_verified or fetched > now or observed > now:
            continue
        if not isinstance(row.get("evidence"), dict):
            continue
        clusters = row["evidence"].get("trend_clusters")
        cluster = clusters[0] if isinstance(clusters, list) and clusters and isinstance(clusters[0], dict) else {}
        categories = cluster.get("categories", [])
        category_names = []
        if isinstance(categories, list):
            for item in categories:
                name = item.get("name") if isinstance(item, dict) else item
                if isinstance(name, str):
                    category_names.append(name)
        category_text = " ".join(category_names).casefold()
        preferred = any(name in category_text for name in ("fashion", "home", "living"))
        active = cluster.get("active") is True
        increase = cluster.get("increase_percentage")
        increase = increase if type(increase) in {int, float} and 0 <= increase <= 1_000_000_000 else 0
        reason = "PREFERRED_INDUSTRY_SIGNAL" if preferred else "SAVED_TREND_CANDIDATE"
        row_value = BingCandidate(keyword, digest, GOOGLE_SOURCE_ID, observed, fetched, reason)
        rank = (not preferred, not active, -increase, -observed.timestamp(), digest)
        if digest not in prepared or rank < prepared[digest][0]:
            prepared[digest] = (rank, row_value)
    selected = sorted(prepared.values(), key=lambda item: item[0])[:limit]
    return tuple(item[1] for item in selected)


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return parsed.astimezone(timezone.utc)


def _context(candidate: BingCandidate, policy_fingerprint: str, started_at: datetime) -> QueryContext:
    capability = source_capability(BING_SOURCE_ID)
    scope = f"saved:{candidate.source_id}:{candidate.reason}"
    payload = {"plan_id": PLAN_ID, "version": PLAN_VERSION,
               "source_id": BING_SOURCE_ID, "provider_id": capability.provider_id,
               "query": candidate.keyword, "scope": scope, "market": "US", "language": "en-US"}
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
    return QueryContext(BING_SOURCE_ID, capability.provider_id, PLAN_ID, PLAN_VERSION,
                        policy_fingerprint, fingerprint, candidate.keyword, scope,
                        started_at.astimezone(timezone.utc))


def build_verification_plan(rows: tuple[dict, ...], *, policy_fingerprint: str,
                            started_at: datetime, recently_verified: frozenset[str] = frozenset(),
                            limit: int = MAX_BING_VERIFICATIONS) -> BingVerificationPlan:
    if (not isinstance(policy_fingerprint, str) or len(policy_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in policy_fingerprint)
            or not isinstance(started_at, datetime) or started_at.tzinfo is None
            or started_at.utcoffset() is None):
        raise ValueError("validated Bing policy and timezone-aware start are required")
    candidates = select_saved_candidates(rows, now=started_at,
                                         recently_verified=recently_verified, limit=limit)
    return BingVerificationPlan(policy_fingerprint, started_at.astimezone(timezone.utc),
                                candidates, tuple(_context(item, policy_fingerprint, started_at)
                                                  for item in candidates))


def validate_dynamic_context(context: QueryContext, *, policy_fingerprint: str) -> bool:
    if (not isinstance(context, QueryContext) or context.source_id != BING_SOURCE_ID
            or context.provider_id != source_capability(BING_SOURCE_ID).provider_id
            or context.plan_id != PLAN_ID or context.plan_version != PLAN_VERSION
            or context.plan_fingerprint != policy_fingerprint
            or not isinstance(context.started_at, datetime) or context.started_at.tzinfo is None):
        return False
    try:
        normalized, identity = normalize_keyword(context.canonical_query)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        allowed_scopes = {f"saved:{GOOGLE_SOURCE_ID}:PREFERRED_INDUSTRY_SIGNAL",
                          f"saved:{GOOGLE_SOURCE_ID}:SAVED_TREND_CANDIDATE"}
        expected = _context(BingCandidate(normalized, digest, GOOGLE_SOURCE_ID,
                                          context.started_at, context.started_at,
                                          context.taxonomy_scope.split(":", 2)[-1]),
                            policy_fingerprint, context.started_at)
        return context.taxonomy_scope in allowed_scopes and context == expected
    except (TypeError, ValueError, AttributeError):
        return False
