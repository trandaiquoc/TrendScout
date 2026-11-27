"""Read projections that keep source-specific evidence and rankings distinct."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone

from app.sources.catalog import (
    GOOGLE_SOURCE_ID,
    SOURCE_CAPABILITIES,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
)


def _object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _same_instant(first, second):
    def parse(value):
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, str):
            try:
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if result.tzinfo is None or result.utcoffset() is None:
            return None
        return result.astimezone(timezone.utc)

    left, right = parse(first), parse(second)
    return left is not None and left == right


def _age_seconds(value, now):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return max(0, int((now - value.astimezone(timezone.utc)).total_seconds()))


def _market_scope(row, scope):
    geography = _object(row.get("geography"))
    field = geography.get("field")
    value = geography.get("value")
    normalized = value.casefold().strip() if isinstance(value, str) else ""
    scope_market = scope.get("market")
    scope_region = scope.get("region") or scope.get("regionCode")
    if ((field == "country_scope" and normalized == "us")
            or scope.get("country_scope") == "US"):
        return "US_COUNTRY_SCOPED"
    if (normalized in {"global", "world", "worldwide"}
            or isinstance(scope_market, str)
            and scope_market.casefold().strip() in {"global", "world", "worldwide"}):
        return "GLOBAL"
    if ((field in {"region", "regionCode"} and normalized == "us")
            or (field == "market" and normalized in {"us", "en-us"})
            or isinstance(scope_region, str) and scope_region.casefold() == "us"):
        return "US_FILTER_ONLY"
    return "UNKNOWN"


def _platform_and_role(source_id, scope):
    try:
        capability = source_capability(source_id)
    except (TypeError, ValueError):
        capability = None
    evidence_type = scope.get("evidence_kind", "provider_record")
    if evidence_type == "search_verification":
        return (getattr(capability, "platform_id", source_id), "VERIFICATION")
    if (source_id == GOOGLE_SOURCE_ID
            and scope.get("data_source") == "GOOGLE_TRENDS"):
        return "google_trends", "TREND_SIGNAL"
    if source_id == WIKIMEDIA_SOURCE_ID:
        return "wikimedia", "OBSERVED_ACTIVITY"
    if source_id == YOUTUBE_SOURCE_ID:
        return "youtube", "VIDEO_EVIDENCE_ONLY"
    roles = getattr(capability, "evidence_roles", ())
    return (getattr(capability, "platform_id", source_id),
            roles[0] if roles else "EVIDENCE_ONLY")


def _independence_key(source_id, provider_id, platform_id, role, evidence_type, scope):
    transport = scope.get("transport_id")
    if role == "VERIFICATION":
        capability = None
        try:
            capability = source_capability(source_id)
        except (TypeError, ValueError):
            pass
        domain = (transport if isinstance(transport, str) and transport.strip()
                  else getattr(capability, "independence_domain", None)
                  or provider_id or source_id)
        if "serpapi" in domain.casefold():
            domain = "serpapi_search_verification"
        return f"verification:{domain}"
    try:
        capability = source_capability(source_id)
    except (TypeError, ValueError):
        capability = None
    domain = getattr(capability, "independence_domain", None) or platform_id
    return f"signal:{role}:{domain}"


def _metric_observations(source_row, evidence_row, scope):
    evidence = _object(source_row.get("evidence"))
    source_id = evidence_row["source_id"]
    collected_at = evidence_row.get("collected_at")
    metrics = []
    if source_id == WIKIMEDIA_SOURCE_ID:
        if (evidence.get("observation_date") == scope.get("observation_date")
                and _same_instant(evidence.get("fetched_at"), collected_at)):
            for metric, unit, value in (
                ("views_ceil", "approx_pageviews_upper_bound", evidence.get("views_ceil")),
                ("provider_rank", "ordinal_top_list_position", evidence.get("provider_rank")),
            ):
                if type(value) is int and value >= 0:
                    metrics.append({"metric": metric, "unit": unit, "value": value})
    elif (source_id == GOOGLE_SOURCE_ID
          and scope.get("data_source") == "GOOGLE_TRENDS"):
        if (evidence.get("provider_data_at") == scope.get("provider_data_at")
                and _same_instant(evidence.get("fetched_at"), collected_at)):
            started_values = scope.get("trend_cluster_started_at", ())
            if not isinstance(started_values, list):
                started_values = [started_values]
            clusters = evidence.get("trend_clusters", ())
            for cluster in clusters if isinstance(clusters, list) else ():
                if not isinstance(cluster, dict) or cluster.get("started_at") not in started_values:
                    continue
                percentage = cluster.get("increase_percentage")
                if type(percentage) in {int, float}:
                    metrics.append({
                        "metric": "provider_increase_percentage",
                        "unit": "provider_reported_percentage",
                        "value": percentage,
                    })
                volume = cluster.get("search_volume")
                if type(volume) is int and volume >= 0:
                    metrics.append({
                        "metric": "approximate_search_volume_bucket",
                        "unit": cluster.get("search_volume_semantics")
                        or "provider_defined_approximate_bucket",
                        "value": volume,
                        "display": cluster.get("search_volume_display"),
                    })
    return metrics


def _trend_outcome(result):
    algorithm_id = result.get("algorithm_id")
    payload = _object(result.get("result_json"))
    value = result.get("value")
    if result.get("status") != "COMPLETED":
        return None
    if algorithm_id == "google-trends-increase-rank":
        increase = payload.get("provider_reported_increase_percentage")
        if type(increase) not in {int, float}:
            return None
        return {
            "direction": "INCREASING" if increase > 0
            else "DECLINING" if increase < 0 else "STABLE",
            "comparison_contract": ["provider_increase_percentage",
                                    "provider_reported_percentage", 1],
        }
    if algorithm_id == "growth-ratio" and type(value) in {int, float}:
        return {
            "direction": "INCREASING" if value > 1 else "DECLINING" if value < 1 else "STABLE",
            "comparison_contract": [payload.get("input_metric"), payload.get("input_unit"),
                                    payload.get("window_days")],
        }
    if algorithm_id == "wikimedia-trend":
        status = payload.get("legacy_status")
        direction = {"EMERGING": "INCREASING", "FALLING": "DECLINING",
                     "STABLE": "STABLE"}.get(status)
        if direction:
            return {"direction": direction, "comparison_contract": None}
    return None


def build_multi_source_profile(*, canonical_keyword, canonical_identity, data_mode,
                               keyword_rows, evidence_rows, algorithm_results, now):
    """Project active lineage into a provenance-aware profile; never compose raw metrics."""
    if (not isinstance(canonical_keyword, str) or not canonical_keyword.strip()
            or not isinstance(canonical_identity, str) or len(canonical_identity) != 64
            or data_mode not in {"LIVE", "SYNTHETIC"}
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("canonical keyword, identity, mode and aware time are required")
    now = now.astimezone(timezone.utc)
    keywords = {row["id"]: row for row in keyword_rows if isinstance(row, dict)}
    results_by_keyword = defaultdict(list)
    for result in algorithm_results:
        if isinstance(result, dict):
            results_by_keyword[result.get("keyword_id")].append(result)

    groups = {}
    market_values = set()
    latest_collection = oldest_collection = None
    signal_outcomes = []
    signal_outcome_keys = set()
    for row in sorted(evidence_rows, key=lambda item: (item.get("collected_at") or "", item["id"])):
        source_id = row["source_id"]
        scope = _object(row.get("query_scope"))
        provider_id = scope.get("provider_id")
        try:
            capability = source_capability(source_id)
        except (TypeError, ValueError):
            capability = None
        provider_id = provider_id or getattr(capability, "provider_id", source_id)
        platform_id, role = _platform_and_role(source_id, scope)
        evidence_type = scope.get("evidence_kind", "provider_record")
        adapter_profile = scope.get("discovery_mode") or evidence_type
        adapter_id = f"{source_id}:{adapter_profile}"
        independence_key = _independence_key(
            source_id, provider_id, platform_id, role, evidence_type, scope,
        )
        identity = (source_id, adapter_id, platform_id, provider_id,
                    evidence_type, role, independence_key)
        item = groups.setdefault(identity, {
            "source_id": source_id,
            "source_name": row.get("display_name") or source_id,
            "source_adapter_id": adapter_id,
            "provider_id": provider_id,
            "transport_id": scope.get("transport_id"),
            "platform_id": platform_id,
            "evidence_type": evidence_type,
            "evidence_role": role,
            "independent_evidence_group_id": hashlib.sha256(
                independence_key.encode("utf-8")
            ).hexdigest(),
            "data_mode": data_mode,
            "evidence_references": [],
            "metric_contracts": [],
            "metric_observations": [],
            "source_specific_rankings": [],
            "geography": [],
            "freshness": [],
            "processing_permission": "NOT_RECORDED",
            "limitations": [],
        })
        source_row = keywords.get(row.get("keyword_id"), {})
        ref = {
            "evidence_id": row["id"],
            "content_digest": row.get("content_digest"),
            "observed_at": (row["observed_at"].isoformat()
                            if isinstance(row.get("observed_at"), datetime)
                            else row.get("observed_at")),
            "collected_at": (row["collected_at"].isoformat()
                             if isinstance(row.get("collected_at"), datetime)
                             else row.get("collected_at")),
            "expires_at": (row["expires_at"].isoformat()
                           if isinstance(row.get("expires_at"), datetime)
                           else row.get("expires_at")),
            "query_scope": scope,
            "geography": _object(row.get("geography")),
        }
        item["evidence_references"].append(ref)
        market = _market_scope(row, scope)
        market_values.add(market)
        item["geography"].append(market)
        item["metric_contracts"].append({
            "metric": scope.get("source_metric_name"),
            "unit": scope.get("source_metric_unit"),
        })
        item["metric_observations"].extend(
            _metric_observations(source_row, row, scope)
        )
        collected_at = row.get("collected_at")
        if isinstance(collected_at, datetime):
            collected_at = collected_at.astimezone(timezone.utc)
            latest_collection = max(latest_collection, collected_at) if latest_collection else collected_at
            oldest_collection = min(oldest_collection, collected_at) if oldest_collection else collected_at
            item["freshness"].append({
                "collected_at": collected_at.isoformat(),
                "age_seconds": _age_seconds(collected_at, now),
                "expires_at": (row["expires_at"].isoformat()
                               if isinstance(row.get("expires_at"), datetime)
                               else row.get("expires_at")),
            })
        for result in results_by_keyword.get(row.get("keyword_id"), ()):
            result_payload = _object(result.get("result_json"))
            # Linked verification evidence can belong to a different source
            # than the keyword's trend result. Keep rankings with their actual
            # algorithm input source.
            if result_payload.get("source_id") != source_id:
                continue
            result_summary = {
                "algorithm_id": result.get("algorithm_id"),
                "version": result.get("version"),
                "status": result.get("status"),
                "reason_code": result.get("reason_code"),
                "value": result.get("value"),
                "metric": result.get("metric"),
                "unit": result.get("unit"),
                "input_digest": result.get("input_digest"),
                "updated_at": (result["updated_at"].isoformat()
                               if isinstance(result.get("updated_at"), datetime)
                               else result.get("updated_at")),
                "result": result_payload,
            }
            if result_summary not in item["source_specific_rankings"]:
                item["source_specific_rankings"].append(result_summary)
            if result.get("reason_code") == "POLICY_RESTRICTED":
                item["processing_permission"] = "POLICY_RESTRICTED"
            elif data_mode == "SYNTHETIC":
                item["processing_permission"] = "NOT_REQUIRED_FOR_SYNTHETIC"
            outcome = (_trend_outcome(result)
                       if role in {"TREND_SIGNAL", "OBSERVED_ACTIVITY"} else None)
            outcome_key = (item["independent_evidence_group_id"],
                           result.get("algorithm_id"), result.get("input_digest"))
            if outcome and outcome_key not in signal_outcome_keys:
                signal_outcome_keys.add(outcome_key)
                signal_outcomes.append({
                    "independent_evidence_group_id": item["independent_evidence_group_id"],
                    "source_id": source_id,
                    "algorithm_id": result.get("algorithm_id"),
                    **outcome,
                })
        if role == "OBSERVED_ACTIVITY":
            item["limitations"].extend(("TOP_LIST_CENSORED", "APPROXIMATE_VALUE"))
        elif platform_id == "google_trends":
            item["limitations"].extend((
                "US_FILTER_DOES_NOT_PROVE_AUDIENCE_RESIDENCE",
                "APPROXIMATE_VOLUME_BUCKET_IS_NOT_EXACT_SEARCH_COUNT",
                "SNAPSHOT_IS_NOT_COMPLETE_DAILY_HISTORY",
            ))
        elif role == "VERIFICATION":
            item["limitations"].append("SERP_POSITION_IS_NOT_SEARCH_DEMAND")
        item["limitations"].extend(getattr(capability, "known_limitations", ()))

        # Google Trends may carry a separately scoped Google Search check in the
        # keyword evidence. Keep it as verification and link it to the parent
        # retained field rather than presenting a SERP rank as demand.
        google_verification = _object(source_row.get("evidence", {}).get("serp_verification")
                                      if isinstance(source_row.get("evidence"), dict) else None)
        if (platform_id == "google_trends"
                and _same_instant(source_row.get("evidence", {}).get("fetched_at")
                                  if isinstance(source_row.get("evidence"), dict) else None,
                                  row.get("collected_at"))
                and google_verification
                and (google_verification.get("status") or google_verification.get("results"))):
            verification_scope = {
                "evidence_kind": "search_verification",
                "data_mode": data_mode,
                "query": google_verification.get("query"),
                "market": google_verification.get("market") or google_verification.get("region"),
                "transport_id": google_verification.get("transport_id") or "serpapi",
            }
            verification_provider = google_verification.get("provider_id", "serpapi_google")
            verification_group = _independence_key(
                GOOGLE_SOURCE_ID, verification_provider, "google_search", "VERIFICATION",
                "search_verification", verification_scope,
            )
            verification_adapter_id = "google_web_serp:serp_verification"
            verification_identity = (
                GOOGLE_SOURCE_ID, verification_adapter_id, "google_search",
                verification_provider, "search_verification", "VERIFICATION",
                verification_group,
            )
            verification_item = groups.setdefault(verification_identity, {
                "source_id": GOOGLE_SOURCE_ID,
                "source_name": "Google Search",
                "source_adapter_id": verification_adapter_id,
                "provider_id": verification_provider,
                "transport_id": verification_scope["transport_id"],
                "platform_id": "google_search",
                "evidence_type": "search_verification",
                "evidence_role": "VERIFICATION",
                "independent_evidence_group_id": hashlib.sha256(
                    verification_group.encode("utf-8")
                ).hexdigest(),
                "data_mode": data_mode,
                "evidence_references": [], "metric_contracts": [],
                "metric_observations": [], "source_specific_rankings": [],
                "geography": [], "freshness": [],
                "processing_permission": "VERIFICATION_ONLY", "limitations": [],
            })
            parent_reference = {
                "evidence_id": row["id"],
                "reference_type": "RETAINED_PARENT_FIELD",
                "field_path": ["serp_verification"],
                "content_digest": row.get("content_digest"),
                "expires_at": (row["expires_at"].isoformat()
                               if isinstance(row.get("expires_at"), datetime)
                               else row.get("expires_at")),
            }
            if parent_reference not in verification_item["evidence_references"]:
                verification_item["evidence_references"].append(parent_reference)
            verification_market = verification_scope["market"]
            if isinstance(verification_market, str) and verification_market.casefold() == "us":
                verification_item["geography"].append("US_FILTER_ONLY")
                market_values.add("US_FILTER_ONLY")
            else:
                verification_item["geography"].append("UNKNOWN")
                market_values.add("UNKNOWN")
            verification_item["metric_contracts"].append({
                "metric": "result_rank" if google_verification.get("results") else None,
                "unit": "ordinal result position" if google_verification.get("results") else None,
            })
            if isinstance(google_verification.get("fetched_at"), str):
                verification_item["freshness"].append({
                    "collected_at": google_verification["fetched_at"],
                    "age_seconds": _age_seconds(google_verification["fetched_at"], now),
                    "expires_at": parent_reference.get("expires_at"),
                })
            verification_item["limitations"].append("SERP_POSITION_IS_NOT_SEARCH_DEMAND")

    grouped_by_independence = defaultdict(list)
    for item in groups.values():
        refs = {ref["evidence_id"] for ref in item["evidence_references"]}
        group_id = item["independent_evidence_group_id"]
        entry = grouped_by_independence[group_id]
        if not entry:
            entry.append({
                "id": group_id,
                "role": item["evidence_role"],
                "platform_id": item["platform_id"],
                "provider_ids": [],
                "source_adapter_ids": [],
                "evidence_ids": [],
                "counts_as_independent_trend_signal": item["evidence_role"] == "TREND_SIGNAL",
            })
        summary = entry[0]
        summary["provider_ids"] = sorted(set(summary["provider_ids"] + [item["provider_id"]]))
        summary["source_adapter_ids"] = sorted(set(
            summary["source_adapter_ids"] + [item["source_adapter_id"]]
        ))
        summary["evidence_ids"] = sorted(set(summary["evidence_ids"] + list(refs)))
    independent_groups = sorted((rows[0] for rows in grouped_by_independence.values()),
                                key=lambda item: item["id"])

    source_breakdown = list(groups.values())
    for capability in SOURCE_CAPABILITIES:
        profiles = [(capability.platform_id,
                     role) for role in capability.evidence_roles] or [
                         (capability.platform_id, "EVIDENCE_ONLY")
                     ]
        if capability.source_id == GOOGLE_SOURCE_ID:
            profiles.append(("google_trends", "TREND_SIGNAL"))
        for platform_id, role in profiles:
            if any(item["source_id"] == capability.source_id
                   and item["platform_id"] == platform_id
                   and item["evidence_role"] == role for item in source_breakdown):
                continue
            restricted = role in capability.policy_restricted_roles
            source_breakdown.append({
                "source_id": capability.source_id,
                "source_name": capability.display_name,
                "source_adapter_id": capability.source_id,
                "provider_id": capability.provider_id,
                "transport_id": capability.independence_domain,
                "platform_id": platform_id,
                "evidence_type": None,
                "evidence_role": role,
                "evidence_status": "POLICY_RESTRICTED" if restricted else "NO_STORED_EVIDENCE",
                "data_mode": data_mode,
                "evidence_references": [],
                "coverage": {
                    "keyword_evidence_reference_count": 0,
                    "snapshot_statuses": [],
                    "source_population_totals": "NOT_RETAINED",
                },
                "metric_contracts": [{"metric": capability.metric_name,
                                      "unit": capability.metric_unit}],
                "metric_observations": [],
                "source_specific_rankings": [],
                "geography": [],
                "freshness": [],
                "processing_permission": "POLICY_RESTRICTED" if restricted else "NOT_RECORDED",
                "absence_is_negative_evidence": False,
                "limitations": list(capability.known_limitations),
            })
    for item in source_breakdown:
        item.setdefault("evidence_status", "PRESENT")
        item["evidence_references"].sort(key=lambda ref: (ref.get("collected_at") or "",
                                                           ref["evidence_id"]))
        item.setdefault("coverage", {
            "keyword_evidence_reference_count": len(item["evidence_references"]),
            "snapshot_statuses": sorted({
                ref.get("query_scope", {}).get("snapshot_status")
                for ref in item["evidence_references"]
                if ref.get("query_scope", {}).get("snapshot_status")
            }),
            "source_population_totals": "NOT_RETAINED",
        })
        item["metric_contracts"] = sorted({
            (contract.get("metric"), contract.get("unit"))
            for contract in item["metric_contracts"]
        }, key=lambda value: tuple(str(part or "") for part in value))
        item["metric_contracts"] = [
            {"metric": metric, "unit": unit} for metric, unit in item["metric_contracts"]
        ]
        item["metric_observations"].sort(key=lambda metric: (
            metric.get("metric", ""), str(metric.get("value", "")),
        ))
        item["source_specific_rankings"].sort(key=lambda result: (
            result.get("algorithm_id", ""), result.get("version", ""),
        ))
        item["geography"] = sorted(set(item["geography"]))
        item["freshness"].sort(key=lambda value: value.get("collected_at", ""))
        item["limitations"] = sorted(set(item["limitations"]))
        if item["evidence_role"] == "VERIFICATION":
            item["independence_note"] = (
                "Verification evidence does not count as a trend-demand signal."
            )
        if item["evidence_role"] == "OBSERVED_ACTIVITY":
            item["independence_note"] = (
                "Popularity/activity requires a compatible source-specific trend method "
                "before it counts as trend corroboration."
            )

    trend_group_ids = {item["independent_evidence_group_id"] for item in signal_outcomes}
    comparison_contracts = defaultdict(lambda: defaultdict(set))
    for outcome in signal_outcomes:
        contract = outcome.get("comparison_contract")
        if contract and all(value is not None for value in contract):
            comparison_contracts[tuple(contract)][
                outcome["independent_evidence_group_id"]
            ].add(outcome["direction"])
    conflicting = [
        {"metric_contract": list(contract), "directions": sorted(directions)}
        for contract, groups_by_id in sorted(comparison_contracts.items(), key=lambda item: str(item[0]))
        for directions in [{direction for values in groups_by_id.values() for direction in values}]
        if len(groups_by_id) >= 1 and len(directions) > 1
    ]
    if conflicting:
        corroboration_status = "CONFLICTING_COMPARABLE_SIGNALS"
    elif len(trend_group_ids) >= 2:
        corroboration_status = "MULTIPLE_INDEPENDENT_TREND_SIGNALS"
    elif len(trend_group_ids) == 1:
        corroboration_status = "SINGLE_INDEPENDENT_TREND_SIGNAL"
    elif any(item["evidence_role"] == "TREND_SIGNAL"
             and item["evidence_status"] == "PRESENT" for item in source_breakdown):
        corroboration_status = "TREND_SIGNAL_NOT_ASSESSED"
    else:
        corroboration_status = "NO_TREND_SIGNAL_EVIDENCE"

    market_status = (next(iter(market_values)) if len(market_values) == 1
                     else "MIXED" if market_values else "UNKNOWN")
    limitations = {
        "ABSENT_SOURCE_IS_NOT_NEGATIVE_EVIDENCE",
        "METRICS_ARE_NOT_COMBINED_ACROSS_UNITS_OR_SOURCES",
        "NO_CALIBRATED_CONFIDENCE_OR_PROBABILITY",
        "NO_COMPOSITE_RANKING",
        "LEXICAL_CANONICAL_IDENTITY_IS_NOT_ENTITY_RESOLUTION",
    }
    for item in source_breakdown:
        limitations.update(item["limitations"])
    return {
        "canonical_keyword": canonical_keyword,
        "canonical_keyword_identity": canonical_identity,
        "source_keyword_variants": sorted({
            row.get("keyword", "") for row in keyword_rows if row.get("keyword")
        }, key=lambda value: (value.casefold(), value)),
        "data_mode": data_mode,
        "source_breakdown": sorted(source_breakdown, key=lambda item: (
            item["platform_id"], item["source_id"], item["source_adapter_id"],
        )),
        "independent_evidence_groups": independent_groups,
        "market_relevance": {
            "status": market_status,
            "evidence_scopes": sorted(market_values),
            "audience_geography_inferred": False,
        },
        "freshness": {
            "latest_collected_at": latest_collection.isoformat() if latest_collection else None,
            "oldest_collected_at": oldest_collection.isoformat() if oldest_collection else None,
            "latest_age_seconds": _age_seconds(latest_collection, now),
            "retention_days": 7,
        },
        "trend_signal_outcomes": sorted(signal_outcomes, key=lambda item: (
            item["independent_evidence_group_id"], item["algorithm_id"],
        )),
        "corroboration": {
            "status": corroboration_status,
            "independent_trend_signal_group_count": len(trend_group_ids),
            "conflicting_evidence": conflicting,
            "comparable_metric_contracts": [list(contract) for contract in sorted(
                comparison_contracts.keys(), key=str,
            )],
            "composite_score": None,
            "ranking_method": "SOURCE_SPECIFIC_RESULTS_ONLY",
        },
        "limitations": sorted(limitations),
    }
