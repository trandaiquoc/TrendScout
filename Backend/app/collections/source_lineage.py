"""Build minimal, deterministic lineage rows for provider Fetch evidence.

The full provider record is already retained in existing business fields. These
rows keep only a digest and scoped provenance so later retention work can find
the dependent copies without duplicating provider payloads.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone

from app.collections.store import keyword_record
from app.retention.source_policy import (
    effective_source_retention_deadline,
    source_retention_policy,
)
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_CONTEXT_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    source_capability,
)

_INTERNAL_SOURCE_NAMES = {WIKIMEDIA_CONTEXT_SOURCE_ID: "Wikimedia entity context"}
_ENTITY_CONTEXT_COLUMNS = (
    "keyword_id", "title", "page_id", "entity_id", "description", "page_namespace",
    "is_disambiguation", "redirected_from", "page_language", "is_main_page",
    "is_main_page_known", "entity_type", "occupations", "category_ids", "instance_types",
    "source_url", "method", "status", "context_version", "captured_at",
)

_SCOPE_FIELDS = (
    "access", "canonical_keyword_identity", "context_version", "country_scope",
    "data_source", "discovery_mode", "entity_id", "entity_type", "is_disambiguation",
    "is_main_page", "is_main_page_known", "language", "market", "method",
    "observation_date", "page_id", "page_language", "page_namespace", "plan_id",
    "plan_version", "project", "provider_id", "query", "redirected_from", "region",
    "regionCode", "source_url", "status", "time_window_hours", "title", "transport_id",
    "verification_mode",
)
_OBSERVATION_FIELDS = (
    "observed_at", "observation_date", "provider_data_at", "provider_created_at",
)
_GEOGRAPHY_FIELDS = {
    "country_scope": "observation_geography",
    "region": "provider_region_filter",
    "regionCode": "provider_region_filter",
    "market": "provider_market_localization",
}
_GOOGLE_KEYWORD_FIELDS = frozenset({
    "data_source", "discovery_mode", "provider_id", "transport_id",
    "canonical_keyword_identity", "region", "language", "time_window_hours",
    "provider_data_at", "provider_created_at", "fetched_at", "source_url",
    "delivery_state", "trend_clusters", "serp_verification", "quality_flags",
})
_WIKIMEDIA_KEYWORD_FIELDS = frozenset({
    "article_original", "article_display", "provider_rank", "views_ceil",
    "country_scope", "access", "observation_date", "fetched_at", "source_url",
    "quality_flags",
})


def _json_value(value):
    """Convert supported timestamp values without accepting arbitrary objects."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite provider value")
        return value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("provider object has a non-string key")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise ValueError("provider record contains an unsupported value")


def _canonical_json(value):
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(",", ":"))


def _keyword_field_lineage(source_id, keyword_id, content, *, evidence_kind):
    """Describe exact provider-derived keyword fields without copying their values."""
    if not isinstance(keyword_id, str) or not keyword_id or not isinstance(content, dict):
        return []
    if source_id == GOOGLE_SOURCE_ID and evidence_kind == "provider_record":
        fields = _GOOGLE_KEYWORD_FIELDS
        values = content
    elif source_id == WIKIMEDIA_SOURCE_ID and evidence_kind == "provider_record":
        fields = _WIKIMEDIA_KEYWORD_FIELDS
        values = content
    elif source_id == BING_SOURCE_ID and evidence_kind == "search_verification":
        fields = frozenset({"bing_verification"})
        values = {"bing_verification": content}
    else:
        return []
    result = []
    for field in sorted(fields):
        if field not in values:
            continue
        try:
            value_digest = hashlib.sha256(
                _canonical_json(values[field]).encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError, OverflowError, UnicodeError):
            return []
        result.append({
            "keyword_id": keyword_id,
            "path": [field],
            "value_sha256": value_digest,
        })
    return result


def _entity_context_lineage(keyword_id, content):
    """Fingerprint one provider-derived entity-context row for exact invalidation."""
    if not isinstance(keyword_id, str) or not keyword_id or not isinstance(content, dict):
        return None
    collected_at = _aware_timestamp(content.get("fetched_at"))
    if collected_at is None:
        return None
    try:
        value_digest = hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return None
    return {
        "keyword_id": keyword_id,
        "context_version": content.get("context_version"),
        "collected_at": collected_at.isoformat(),
        "value_sha256": value_digest,
    }


def _aware_timestamp(value):
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


def _scope(content, *, data_mode, snapshot_status, evidence_kind):
    scope = {
        "scope_version": 1,
        "evidence_kind": evidence_kind,
    }
    if isinstance(data_mode, str):
        scope["data_mode"] = data_mode
    if isinstance(snapshot_status, str):
        scope["snapshot_status"] = snapshot_status
    for field in (*_SCOPE_FIELDS, *_OBSERVATION_FIELDS):
        value = content.get(field)
        if value is not None:
            scope[field] = _json_value(value)
    clusters = content.get("trend_clusters")
    if isinstance(clusters, list):
        started = [item.get("started_at") for item in clusters
                   if isinstance(item, dict) and item.get("started_at") is not None]
        if started:
            scope["trend_cluster_started_at"] = _json_value(started)
    return scope


def _observed_at(content):
    for field in _OBSERVATION_FIELDS:
        parsed = _aware_timestamp(content.get(field))
        if parsed is not None:
            return parsed
    clusters = content.get("trend_clusters")
    if isinstance(clusters, list):
        values = [_aware_timestamp(item.get("started_at")) for item in clusters
                  if isinstance(item, dict)]
        values = [value for value in values if value is not None]
        if values:
            return min(values)
    return None


def _geography(content):
    for field, meaning in _GEOGRAPHY_FIELDS.items():
        value = content.get(field)
        if isinstance(value, str) and value.strip():
            return {"field": field, "value": value, "meaning": meaning}
    return None


def _external_id(content):
    for field in ("provider_video_id", "canonical_keyword_identity", "query_fingerprint", "entity_id"):
        value = content.get(field)
        if isinstance(value, str) and value.strip():
            return value[:512]
    if (isinstance(content.get("project"), str)
            and isinstance(content.get("article_original"), str)):
        return f"{content['project']}:{content['article_original']}"[:512]
    if type(content.get("page_id")) is int and content["page_id"] > 0:
        return str(content["page_id"])
    return None


def _source_display_name(source_id):
    if source_id in _INTERNAL_SOURCE_NAMES:
        return _INTERNAL_SOURCE_NAMES[source_id]
    return source_capability(source_id).display_name


def _context_source_records(rows):
    records = []
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if not isinstance(row, (list, tuple)) or len(row) != len(_ENTITY_CONTEXT_COLUMNS):
            records.append(None)
            continue
        record = dict(zip(_ENTITY_CONTEXT_COLUMNS, row))
        for field in ("occupations", "category_ids", "instance_types"):
            if isinstance(record[field], str):
                try:
                    record[field] = json.loads(record[field])
                except (TypeError, ValueError):
                    pass
        record["fetched_at"] = record.pop("captured_at")
        records.append(record)
    return records


def build_source_evidence(sources, *, bing_updates=(), entity_context_rows=()):
    """Return idempotent evidence descriptors and the number left untracked.

    Collection time is accepted only from an explicit per-record ``fetched_at``
    timestamp. A date-only observation remains text in ``query_scope`` and is
    never converted into an invented instant. Payloads stay NULL by design.
    """
    evidence_by_id = {}
    untracked_records = 0

    def add(source_id, content, *, data_mode, snapshot_status, evidence_kind,
            keyword_id=None, keyword_field_lineage=(), entity_context_lineage=None):
        nonlocal untracked_records
        if not isinstance(content, dict):
            untracked_records += 1
            return
        try:
            display_name = _source_display_name(source_id)
            encoded = _canonical_json(content)
            scope = _scope(content, data_mode=data_mode,
                           snapshot_status=snapshot_status, evidence_kind=evidence_kind)
            scope_encoded = _canonical_json(scope)
        except (TypeError, ValueError, OverflowError):
            untracked_records += 1
            return
        collected_at = _aware_timestamp(content.get("fetched_at"))
        if collected_at is None:
            untracked_records += 1
            return
        try:
            policy = source_retention_policy(source_id)
            expires_at = effective_source_retention_deadline(source_id, collected_at)
        except (TypeError, ValueError):
            untracked_records += 1
            return

        content_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        observed_at = _observed_at(content)
        identity = "\n".join((
            source_id,
            content_digest,
            scope_encoded,
            observed_at.isoformat() if observed_at else "",
            collected_at.isoformat(),
        ))
        evidence_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        field_lineage = [
            item for item in keyword_field_lineage
            if isinstance(item, dict)
        ]
        row = evidence_by_id.get(evidence_id)
        if row is None:
            row = {
                "id": evidence_id,
                "source_id": source_id,
                "source_display_name": display_name,
                "external_id": _external_id(content),
                "content_digest": content_digest,
                "query_scope": scope,
                "geography": _geography(content),
                "observed_at": observed_at,
                "collected_at": collected_at,
                "payload": None,
                "keyword_field_lineage": [],
                "entity_context_lineage": [],
                "expires_at": expires_at,
                "retention_policy": policy.metadata(),
                "keyword_ids": set(),
            }
            evidence_by_id[evidence_id] = row
        if isinstance(keyword_id, str) and keyword_id:
            row["keyword_ids"].add(keyword_id)
        if field_lineage:
            existing_lineage = {
                _canonical_json(item): item
                for item in row["keyword_field_lineage"]
            }
            existing_lineage.update({
                _canonical_json(item): item for item in field_lineage
            })
            row["keyword_field_lineage"] = [
                existing_lineage[key] for key in sorted(existing_lineage)
            ]
        if isinstance(entity_context_lineage, dict):
            existing_context = {
                _canonical_json(item): item for item in row["entity_context_lineage"]
            }
            existing_context[_canonical_json(entity_context_lineage)] = entity_context_lineage
            row["entity_context_lineage"] = [
                existing_context[key] for key in sorted(existing_context)
            ]

    source_list = list(sources) if isinstance(sources, (list, tuple)) else []
    context_records = _context_source_records(entity_context_rows)
    if context_records:
        source_list.append({
            "source_id": WIKIMEDIA_CONTEXT_SOURCE_ID,
            "status": "SUCCESS",
            "records": context_records,
        })

    for source in source_list:
        if not isinstance(source, dict):
            untracked_records += 1
            continue
        source_id = source.get("source_id")
        records = source.get("records", [])
        if not isinstance(records, list):
            untracked_records += 1
            continue
        data_mode = source.get("data_mode")
        snapshot_status = source.get("status")
        try:
            _source_display_name(source_id)
        except (TypeError, ValueError):
            untracked_records += len(records)
            continue

        for record in records:
            if not isinstance(record, dict):
                untracked_records += 1
                continue
            if source_id == BING_SOURCE_ID:
                verification = record.get("bing_verification")
                add(BING_SOURCE_ID, verification, data_mode=data_mode,
                    snapshot_status=snapshot_status, evidence_kind="search_verification",
                    keyword_id=record.get("candidate_keyword_id"),
                    keyword_field_lineage=_keyword_field_lineage(
                        BING_SOURCE_ID, record.get("candidate_keyword_id"),
                        verification, evidence_kind="search_verification",
                    ))
                continue

            keyword_id = record.get("keyword_id") if source_id == WIKIMEDIA_CONTEXT_SOURCE_ID else None
            entity_context_target = (
                _entity_context_lineage(keyword_id, record)
                if source_id == WIKIMEDIA_CONTEXT_SOURCE_ID else None
            )
            cleaned = None
            if source_id != WIKIMEDIA_CONTEXT_SOURCE_ID:
                try:
                    cleaned = keyword_record(record, source_id, data_mode or "",
                                             snapshot_status=snapshot_status or "SUCCESS")
                    if cleaned is not None:
                        keyword_id = cleaned["id"]
                except (TypeError, ValueError, KeyError):
                    pass
            add(source_id, record, data_mode=data_mode,
                snapshot_status=snapshot_status, evidence_kind="provider_record",
                keyword_id=keyword_id,
                entity_context_lineage=entity_context_target,
                keyword_field_lineage=_keyword_field_lineage(
                    source_id, keyword_id, record, evidence_kind="provider_record",
                ))

            # Google keyword evidence can contain several attached Bing checks.
            # Index those separately so provider-specific expiry can be applied.
            if source_id == "google_web_serp":
                history = record.get("bing_verification_history", [])
                current = record.get("bing_verification")
                verifications = [*(history if isinstance(history, list) else [])]
                if isinstance(current, dict):
                    verifications.append(current)
                for verification in verifications:
                    add(BING_SOURCE_ID, verification, data_mode=data_mode,
                        snapshot_status=snapshot_status,
                        evidence_kind="search_verification", keyword_id=keyword_id,
                        keyword_field_lineage=(
                            _keyword_field_lineage(
                                BING_SOURCE_ID, keyword_id, verification,
                                evidence_kind="search_verification",
                            ) if verification is current else ()
                        ))

    for update in bing_updates if isinstance(bing_updates, (list, tuple)) else ():
        if not isinstance(update, dict):
            untracked_records += 1
            continue
        add(BING_SOURCE_ID, update.get("verification"), data_mode="LIVE",
            snapshot_status="SUCCESS", evidence_kind="search_verification",
            keyword_id=update.get("candidate_id"),
            keyword_field_lineage=_keyword_field_lineage(
                BING_SOURCE_ID, update.get("candidate_id"), update.get("verification"),
                evidence_kind="search_verification",
            ))

    rows = []
    for row in evidence_by_id.values():
        row["keyword_ids"] = tuple(sorted(row["keyword_ids"]))
        if row["keyword_field_lineage"] or row["entity_context_lineage"]:
            row["payload"] = {
                "retention_lineage": {
                    "version": 1,
                    "keyword_fields": row["keyword_field_lineage"],
                    "entity_context_rows": sorted(
                        row["entity_context_lineage"],
                        key=lambda item: (item["keyword_id"], item["value_sha256"]),
                    ),
                },
            }
        del row["keyword_field_lineage"]
        del row["entity_context_lineage"]
        rows.append(row)
    rows.sort(key=lambda row: (row["source_id"], row["id"]))
    return {"rows": rows, "untracked_record_count": untracked_records}
