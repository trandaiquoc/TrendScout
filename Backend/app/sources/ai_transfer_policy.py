"""Source-specific permissions for transferring normalized data to AI providers.

Fetch access, application retention, derived metrics, AI transfer, and provider
retention are separate permissions. Unknown sources fail closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_CONTEXT_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)

AI_TRANSFER_POLICY_VERSION = "ai-transfer-policy-2026-11-27-v1"


@dataclass(frozen=True)
class AITransferPolicy:
    source_id: str
    public_data_allowed: bool
    strict_private_allowed: bool
    synthetic_demo_allowed: bool
    classification_fields: frozenset[str]
    recommendation_fields: frozenset[str]
    child_fields: Mapping[str, frozenset[str]]
    retention: str
    provider_use: str
    attribution: str
    references: tuple[str, ...]
    reviewed_on: str = "2026-11-27"
    restrictions: tuple[str, ...] = ()


_PUBLIC_CLASSIFICATION_FIELDS = frozenset({
    "id", "canonical_keyword_identity", "display_keyword", "durable_keyword_ids",
    "categories", "source_ids", "source_records", "data_mode", "entity_identity",
    "evidence_ids", "evidence_references", "evidence_coverage",
})
_PUBLIC_RECOMMENDATION_FIELDS = frozenset({
    "keyword_id", "durable_keyword_ids", "canonical_keyword_identity", "keyword",
    "categories", "entity_identity", "data_mode", "source_ids", "source_records",
    "measured_trend_signals", "source_evidence_profile", "market_relevance",
    "multi_source_corroboration", "insufficient_evidence", "limitations", "evidence_ids",
    "evidence_coverage", "evidence_expiry", "product_research_hypothesis",
})
_CHILD_FIELDS = MappingProxyType({
    "source_records": frozenset({
        "source_id", "platform_id", "durable_keyword_ids", "data_mode", "categories",
        "geography_codes", "observed_at_by_keyword", "latest_collected_at",
        "earliest_expires_at", "active_evidence_count",
    }),
    "observed_at_by_keyword": frozenset({"durable_keyword_id", "observed_at"}),
    "entity_identity": frozenset({"status", "entity_ids"}),
    "evidence_references": frozenset({
        "evidence_id", "source_id", "platform_id", "evidence_role", "observed_at",
        "collected_at", "expires_at", "geography", "metric", "unit", "scope",
        "limitations", "durable_keyword_ids",
    }),
    "evidence_coverage": frozenset({
        "active_linked_count", "included_count", "omitted_count", "status",
    }),
    "source_evidence_profile": frozenset({
        "source_id", "platform_id", "evidence_role", "data_mode", "geography",
        "evidence_references", "metric_contracts", "metric_observations",
        "source_specific_results", "limitations",
    }),
    "metric_contracts": frozenset({"metric", "unit"}),
    "metric_observations": frozenset({"metric", "unit", "value"}),
    "source_specific_results": frozenset({
        "algorithm_id", "version", "status", "reason_code", "metric", "unit", "value",
        "updated_at", "source_scope",
    }),
    "market_relevance": frozenset({
        "status", "evidence_scopes", "audience_geography_inferred",
    }),
    "conflicting_evidence": frozenset({"metric_contract", "directions"}),
    "metric_contract": frozenset({"metric", "unit"}),
    "multi_source_corroboration": frozenset({
        "status", "independent_trend_signal_group_count", "conflicting_evidence",
        "comparable_metric_contracts", "ranking_method",
    }),
    "insufficient_evidence": frozenset({"algorithm_id", "status", "reason_code", "reason"}),
    "measured_trend_signals": frozenset({
        "source_id", "algorithm_id", "direction", "comparison_contract",
    }),
    "scope": frozenset({
        "evidence_kind", "data_source", "data_mode", "country_scope", "observation_date",
        "source_metric_name", "source_metric_unit", "snapshot_status", "window_days",
        "trend_cluster_started_at",
    }),
    "source_scope": frozenset({
        "source_id", "observation_date", "input_metric", "input_unit", "status",
        "reason_code", "data_mode", "market_scope", "geography", "window_days",
        "observation_window", "evidence_references",
    }),
    "observation_window": frozenset({"start", "end"}),
    "geography": frozenset({
        "country_code", "country", "region_code", "region", "language_code", "language",
    }),
    "collection": frozenset({"id", "name", "description", "version", "auto_match"}),
})


_CC0_REFERENCES = (
    "https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/documentation/access-policy.html",
    "https://www.mediawiki.org/wiki/API:REST_API/Policies",
)

_POLICIES = (
    AITransferPolicy(
        source_id=WIKIMEDIA_SOURCE_ID,
        public_data_allowed=True,
        strict_private_allowed=True,
        synthetic_demo_allowed=True,
        classification_fields=_PUBLIC_CLASSIFICATION_FIELDS,
        recommendation_fields=_PUBLIC_RECOMMENDATION_FIELDS,
        child_fields=_CHILD_FIELDS,
        retention=("Wikimedia Analytics API data is CC0; no source retention period was found. "
                   "TrendScout's own evidence retention remains capped at 7 days."),
        provider_use=("OpenRouter routes to the pinned Particle AI endpoint; current published "
                      "provider metadata says no training and zero retention. Recheck before use."),
        attribution=("CC0 does not require attribution; preserve the Wikimedia source ID and "
                     "link for traceability."),
        references=_CC0_REFERENCES,
        restrictions=(
            "Only the top-per-country Pageviews metric and normalized page titles are eligible.",
            "The US scope describes Wikimedia pageview geography; it is not general US demand.",
            "Do not transfer raw payloads, content text, credentials, or user-authored context "
            "unless separately classified and approved.",
        ),
    ),
    AITransferPolicy(
        source_id=GOOGLE_SOURCE_ID,
        public_data_allowed=False,
        strict_private_allowed=False,
        synthetic_demo_allowed=True,
        classification_fields=frozenset(),
        recommendation_fields=frozenset(),
        child_fields=MappingProxyType({}),
        retention="Underlying search/Trends retention terms have not been verified for this use.",
        provider_use="No provider-transfer permission established; AI transfer denied.",
        attribution="No AI transfer; source-specific attribution requirements remain unresolved.",
        references=(
            "https://policies.google.com/terms?hl=en-US",
            "https://serpapi.com/legal",
        ),
        restrictions=(
            "Source ID covers Google Search and Google Trends evidence collected through "
            "separate retrieval paths; the transfer grant is not established for either.",
            "Google's current terms restrict automated access that violates machine-readable "
            "instructions; SerpApi terms do not establish a license to transfer underlying "
            "results to an AI provider.",
        ),
    ),
    AITransferPolicy(
        source_id=BING_SOURCE_ID,
        public_data_allowed=False,
        strict_private_allowed=False,
        synthetic_demo_allowed=True,
        classification_fields=frozenset(),
        recommendation_fields=frozenset(),
        child_fields=MappingProxyType({}),
        retention="Underlying search-result retention terms have not been verified for this use.",
        provider_use="No provider-transfer permission established; AI transfer denied.",
        attribution="No AI transfer; source-specific attribution requirements remain unresolved.",
        references=("https://serpapi.com/legal",),
        restrictions=(
            "Search results are retrieved through SerpApi; no source-specific permission to "
            "transfer underlying Bing content to an AI provider was established.",
        ),
    ),
    AITransferPolicy(
        source_id=YOUTUBE_SOURCE_ID,
        public_data_allowed=False,
        strict_private_allowed=False,
        synthetic_demo_allowed=False,
        classification_fields=frozenset(),
        recommendation_fields=frozenset(),
        child_fields=MappingProxyType({}),
        retention="Existing YouTube refresh/deletion obligations remain in force.",
        provider_use="AI transfer denied.",
        attribution="YouTube API attribution and link requirements remain in force.",
        references=(
            "https://developers.google.com/youtube/terms/developer-policies",
            "https://developers.google.com/youtube/terms/api-services-terms-of-service",
        ),
        restrictions=(
            "Keep the existing video-evidence-only and AI-transfer restriction.",
            "YouTube API policies restrict cross-owner data aggregation and independently "
            "derived metrics; do not transfer these records to AI.",
        ),
    ),
    AITransferPolicy(
        source_id=WIKIMEDIA_CONTEXT_SOURCE_ID,
        public_data_allowed=False,
        strict_private_allowed=False,
        synthetic_demo_allowed=True,
        classification_fields=frozenset(),
        recommendation_fields=frozenset(),
        child_fields=MappingProxyType({}),
        retention="Project retention policy applies; page-content license is not established per item.",
        provider_use="AI transfer denied until each content item's license is verified.",
        attribution="Item-specific attribution and share-alike duties may apply.",
        references=(
            "https://www.mediawiki.org/wiki/API:REST_API/Policies",
            "https://attribution.wikimedia.org/attribution-signals/license.html",
        ),
        restrictions=(
            "This source may return article/entity descriptions rather than CC0 Analytics API "
            "metrics; do not infer the Pageviews CC0 grant covers page content.",
        ),
    ),
)

AI_TRANSFER_POLICIES = MappingProxyType({policy.source_id: policy for policy in _POLICIES})


def ai_transfer_policy(source_id: str) -> AITransferPolicy | None:
    return AI_TRANSFER_POLICIES.get(source_id)


def known_ai_source_ids() -> frozenset[str]:
    return frozenset(AI_TRANSFER_POLICIES)


def eligible_source_ids(profile: str) -> frozenset[str]:
    if profile == "PUBLIC_DEMO":
        return frozenset(policy.source_id for policy in _POLICIES
                         if policy.synthetic_demo_allowed)
    if profile == "STRICT_PRIVATE":
        return frozenset(policy.source_id for policy in _POLICIES
                         if policy.strict_private_allowed)
    if profile == "PUBLIC_DATA":
        return frozenset(policy.source_id for policy in _POLICIES
                         if policy.public_data_allowed)
    return frozenset()


def public_input_fields(source_ids, *, task: str) -> frozenset[str]:
    """Return only fields authorized for every source represented in a group."""
    sources = tuple(sorted(set(source_ids)))
    if not sources or task not in {"classification", "recommendation"}:
        return frozenset()
    policies = [AI_TRANSFER_POLICIES.get(source_id) for source_id in sources]
    if any(policy is None or not policy.public_data_allowed for policy in policies):
        return frozenset()
    allowed = [policy.classification_fields if task == "classification"
               else policy.recommendation_fields for policy in policies]
    fields = set(allowed[0])
    for source_fields in allowed[1:]:
        fields.intersection_update(source_fields)
    return frozenset(fields)


def public_child_fields(source_ids, *, field: str) -> frozenset[str]:
    sources = tuple(sorted(set(source_ids)))
    if not sources:
        return frozenset()
    policies = [AI_TRANSFER_POLICIES.get(source_id) for source_id in sources]
    if any(policy is None or not policy.public_data_allowed for policy in policies):
        return frozenset()
    children = [policy.child_fields.get(field, frozenset()) for policy in policies]
    if not children:
        return frozenset()
    allowed = set(children[0])
    for fields in children[1:]:
        allowed.intersection_update(fields)
    return frozenset(allowed)


def project_public_data_payload(value, source_ids, *, task: str) -> dict:
    """Project a built AI input onto the source registry's field allowlist."""
    if not isinstance(value, dict):
        return {}
    allowed = public_input_fields(source_ids, task=task)
    if not allowed:
        return {}
    evidence_ids = set(value.get("evidence_ids", []))

    def project(item, field):
        if field == "evidence_expiry" and isinstance(item, dict):
            return {
                key: stamp for key, stamp in item.items()
                if key in evidence_ids and isinstance(stamp, str) and len(stamp) <= 64
            }
        if isinstance(item, dict):
            children = public_child_fields(source_ids, field=field)
            if not children:
                return None
            return {
                key: projected for key, raw in item.items() if key in children
                if (projected := project(raw, key)) is not None
            }
        if isinstance(item, list):
            if any(isinstance(row, dict) for row in item):
                projected_rows = [project(row, field) for row in item]
                return [row for row in projected_rows if isinstance(row, dict)]
            return item
        if item is None or type(item) in (str, int, float, bool):
            return item
        return None

    return {
        key: projected for key, raw in value.items() if key in allowed
        if (projected := project(raw, key)) is not None
    }
