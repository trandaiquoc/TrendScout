"""Bounded, deterministic AI input projections over durable keyword records."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone

from app.analysis.keyword_identity import canonical_keyword_identity
from app.sources.catalog import (
    GOOGLE_SOURCE_ID,
    source_capability,
)

INPUT_CONTRACT_VERSION = "ai-input-contract-v2"
_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
_REGION_CODE = re.compile(r"^[A-Z]{2}(?:-[A-Z0-9]{1,3})?$")
_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")


def _parse_time(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value):
    parsed = _parse_time(value)
    return parsed.isoformat() if parsed else None


def _safe_geography(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, pattern in (("country_code", _COUNTRY_CODE), ("country", _COUNTRY_CODE),
                         ("region_code", _REGION_CODE), ("region", _REGION_CODE),
                         ("language_code", _LANGUAGE_CODE), ("language", _LANGUAGE_CODE)):
        candidate = value.get(key)
        if isinstance(candidate, str) and pattern.fullmatch(candidate):
            result[key] = candidate
    return result


def _list(value):
    return value if isinstance(value, list) else []


def _active_evidence(record, now, allowed_sources):
    expiries = record.get("evidence_expiry")
    expiries = expiries if isinstance(expiries, dict) else {}
    active_expiries = {}
    for evidence_id, raw_expiry in expiries.items():
        expiry = _parse_time(raw_expiry)
        if (isinstance(evidence_id, str) and evidence_id and expiry is not None
                and expiry > now):
            active_expiries[evidence_id] = expiry.isoformat()

    references = []
    for item in _list(record.get("evidence_context")):
        if not isinstance(item, dict):
            continue
        evidence_id = item.get("evidence_id")
        source_id = item.get("source_id")
        expiry = _parse_time(active_expiries.get(evidence_id))
        context_expiry = _parse_time(item.get("expires_at"))
        if (not isinstance(evidence_id, str) or evidence_id not in active_expiries
                or expiry is None or expiry <= now or source_id not in allowed_sources
                or context_expiry is not None and context_expiry != expiry):
            continue
        scope = item.get("query_scope") if isinstance(item.get("query_scope"), dict) else {}
        try:
            capability = source_capability(source_id)
        except (TypeError, ValueError):
            continue
        if record.get("data_mode") not in capability.supported_data_modes:
            continue
        scope_mode = scope.get("data_mode")
        if scope_mode is not None and scope_mode != record.get("data_mode"):
            continue
        platform = ("google_trends" if source_id == GOOGLE_SOURCE_ID
                    and scope.get("data_source") == "GOOGLE_TRENDS" else capability.platform_id)
        role = ("VIDEO_EVIDENCE_ONLY" if "VIDEO_EVIDENCE_ONLY" in capability.evidence_roles
                else "TREND_SIGNAL" if platform == "google_trends"
                else capability.evidence_roles[0] if capability.evidence_roles else "EVIDENCE_ONLY")
        if scope.get("evidence_kind") == "search_verification":
            role = "VERIFICATION"
        if role in capability.policy_restricted_roles:
            continue
        independence = capability.independence_domain or capability.platform_id or source_id
        group_source = f"{role}:{independence}"
        raw_contract_scope = {key: scope[key] for key in (
            "evidence_kind", "data_source", "data_mode", "observation_date",
            "source_metric_name", "source_metric_unit", "snapshot_status", "window_days",
            "trend_cluster_started_at",
        ) if key in scope}
        contract_scope = {}
        for key, value in raw_contract_scope.items():
            if isinstance(value, str):
                contract_scope[key] = value[:160]
            elif type(value) in (int, bool):
                contract_scope[key] = value
            elif type(value) is float and math.isfinite(value):
                contract_scope[key] = value
            elif (isinstance(value, list) and len(value) <= 20
                  and all(isinstance(item, str) and len(item) <= 80 for item in value)):
                contract_scope[key] = value
        references.append({
            "evidence_id": evidence_id,
            "source_id": source_id,
            "platform_id": platform,
            "evidence_role": role,
            "independent_evidence_group_id": hashlib.sha256(
                group_source.encode("utf-8")
            ).hexdigest(),
            "observed_at": _iso(item.get("observed_at")),
            "collected_at": _iso(item.get("collected_at")),
            "expires_at": expiry.isoformat(),
            "geography": _safe_geography(item.get("geography")),
            "content_digest": item.get("content_digest")
            if isinstance(item.get("content_digest"), str) else None,
            "metric": (scope.get("source_metric_name")
                       if isinstance(scope.get("source_metric_name"), str) else None),
            "unit": (scope.get("source_metric_unit")
                     if isinstance(scope.get("source_metric_unit"), str) else None),
            "scope": contract_scope,
            "limitations": list(capability.known_limitations),
        })
    references.sort(key=lambda item: (item["source_id"], item["collected_at"] or "",
                                      item["evidence_id"]))
    references_by_id = {item["evidence_id"]: item for item in references}
    ids = sorted(set(active_expiries) & set(references_by_id))
    return (ids, {key: active_expiries[key] for key in ids},
            [references_by_id[key] for key in ids], set(active_expiries))


def group_keyword_records(records, *, allowed_sources, now):
    """Group exact canonical phrases in the same data mode; retain every durable ID."""
    observed_now = _parse_time(now)
    if observed_now is None:
        raise ValueError("AI input requires an aware clock")
    allowed_sources = frozenset(allowed_sources)
    grouped = defaultdict(list)
    for record in records:
        if not isinstance(record, dict):
            continue
        text = record.get("normalized") or record.get("keyword")
        try:
            identity = canonical_keyword_identity(text)
        except (TypeError, ValueError):
            continue
        mode = record.get("data_mode")
        if mode not in {"LIVE", "SYNTHETIC"}:
            continue
        entity_id = record.get("entity_id")
        entity_id = entity_id.strip() if isinstance(entity_id, str) and entity_id.strip() else None
        grouped[(identity, mode, entity_id)].append(record)

    result = []
    groups = sorted(grouped.items(), key=lambda item: (
        item[0][0], item[0][1], item[0][2] is not None, item[0][2] or "",
    ))
    for (identity, mode, entity_identity), members in groups:
        members = sorted(members, key=lambda item: item.get("id", ""))
        durable_ids = [item["id"] for item in members
                       if isinstance(item.get("id"), str) and item["id"]]
        if not durable_ids:
            continue
        group_id = "kwg_" + hashlib.sha256(
            (identity + "\0" + mode + "\0" +
             ("resolved:" + entity_identity if entity_identity else "unresolved")).encode("utf-8")
        ).hexdigest()[:32]
        categories = set()
        entity_ids = set()
        evidence_ids, evidence_expiry, evidence_refs, active_linked_ids = set(), {}, {}, set()
        source_profiles = {}
        evidence_members = defaultdict(set)
        display_forms = []
        excluded = set()
        for record in members:
            keyword = record.get("keyword")
            normalized = record.get("normalized") or keyword
            if isinstance(keyword, str) and keyword.strip():
                display_forms.append(" ".join(keyword.split()))
            elif isinstance(normalized, str) and normalized.strip():
                display_forms.append(" ".join(normalized.split()))
            record_categories = record.get("categories", record.get("topic_category_ids", []))
            if isinstance(record_categories, str):
                try:
                    record_categories = json.loads(record_categories)
                except (TypeError, ValueError):
                    record_categories = []
            if isinstance(record_categories, (list, tuple)):
                categories.update(value for value in record_categories
                                  if isinstance(value, str) and value)
            entity_id = record.get("entity_id")
            if isinstance(entity_id, str) and entity_id.strip():
                entity_ids.add(entity_id.strip())
            ids, expiries, references, linked_ids = _active_evidence(
                record, observed_now, allowed_sources,
            )
            evidence_ids.update(ids)
            evidence_expiry.update(expiries)
            for evidence_reference in references:
                evidence_refs[evidence_reference["evidence_id"]] = evidence_reference
                evidence_members[evidence_reference["evidence_id"]].add(record["id"])
            active_linked_ids.update(linked_ids)
            removed = record.get("removed_collection_ids")
            if isinstance(removed, (list, tuple)):
                excluded.update(value for value in removed if isinstance(value, str))
            source_id = record.get("source")
            try:
                capability = source_capability(source_id)
            except (TypeError, ValueError):
                capability = None
            source_key = source_id if isinstance(source_id, str) else "unknown"
            source_record = source_profiles.setdefault(source_key, {
                "source_id": source_key,
                "platform_id": capability.platform_id if capability else "unknown",
                "durable_keyword_ids": [],
                "data_mode": mode,
                "categories": set(),
                "geography_codes": set(),
                "observed_at_by_keyword": [],
                "latest_collected_at": None,
                "earliest_expires_at": None,
                "active_evidence_count": 0,
            })
            source_record["durable_keyword_ids"].append(record["id"])
            source_record["observed_at_by_keyword"].append({
                "durable_keyword_id": record["id"],
                "observed_at": _iso(record.get("observed_at")),
            })
            if isinstance(record_categories, (list, tuple)):
                source_record["categories"].update(value for value in record_categories
                                                   if isinstance(value, str) and value)
            geography = sorted({
                str(code) for reference in references
                for code in _safe_geography(reference.get("geography")).values()
            })
            source_record["geography_codes"].update(geography)
            source_record["active_evidence_count"] += len(ids)
            collected = [_parse_time(reference.get("collected_at")) for reference in references]
            expires = [_parse_time(reference.get("expires_at")) for reference in references]
            collected = [value for value in collected if value]
            expires = [value for value in expires if value]
            if collected:
                latest = max(collected).isoformat()
                if source_record["latest_collected_at"] is None or latest > source_record["latest_collected_at"]:
                    source_record["latest_collected_at"] = latest
            if expires:
                earliest = min(expires).isoformat()
                if source_record["earliest_expires_at"] is None or earliest < source_record["earliest_expires_at"]:
                    source_record["earliest_expires_at"] = earliest
        for evidence_id, reference in evidence_refs.items():
            reference["durable_keyword_ids"] = sorted(evidence_members[evidence_id])
        source_records = []
        for value in sorted(source_profiles.values(), key=lambda item: item["source_id"]):
            source_records.append({
                **value,
                "durable_keyword_ids": sorted(set(value["durable_keyword_ids"])),
                "categories": sorted(value["categories"]),
                "geography_codes": sorted(value["geography_codes"]),
                "observed_at_by_keyword": sorted(value["observed_at_by_keyword"],
                                                  key=lambda item: item["durable_keyword_id"]),
            })
        display_forms.sort(key=lambda value: (value.casefold(), value))
        result.append({
            "id": group_id,
            "canonical_keyword_identity": identity,
            "display_keyword": display_forms[0] if display_forms else identity,
            "durable_keyword_ids": sorted(set(durable_ids)),
            "data_mode": mode,
            "categories": sorted(categories),
            "entity_identity": {
                "status": "MULTIPLE_IDENTITIES" if len(entity_ids) > 1
                else "RESOLVED" if entity_ids else "UNRESOLVED",
                "entity_ids": sorted(entity_ids),
            },
            "source_ids": sorted(
                {item["source_id"] for item in source_records}
                | {item["source_id"] for item in evidence_refs.values()}
            ),
            "source_records": source_records,
            "evidence_ids": sorted(evidence_ids),
            "evidence_expiry": {key: evidence_expiry[key] for key in sorted(evidence_expiry)},
            "evidence_references": [evidence_refs[key] for key in sorted(evidence_refs)],
            "evidence_coverage": {
                "active_linked_count": len(active_linked_ids),
                "included_count": len(evidence_ids),
                "omitted_count": max(0, len(active_linked_ids) - len(evidence_ids)),
                "status": "LIMITED_BY_REFERENCE_CAP"
                if len(active_linked_ids) > len(evidence_ids) else "COMPLETE",
            },
            "removed_collection_ids": sorted(excluded),
            # Private fan-out mapping is deliberately omitted by payload builders.
            "_records": members,
        })
    return result


def group_digest_value(group):
    """Return only contract fields suitable for stable digesting."""
    return {key: group.get(key) for key in (
        "id", "canonical_keyword_identity", "display_keyword", "durable_keyword_ids",
        "data_mode", "categories", "entity_identity", "source_ids", "source_records",
        "evidence_ids", "evidence_expiry", "evidence_references", "removed_collection_ids",
        "evidence_coverage",
    )}


def supported_ai_data_mode(source_id, data_mode):
    try:
        capability = source_capability(source_id)
    except (TypeError, ValueError):
        return False
    return data_mode in capability.supported_data_modes
