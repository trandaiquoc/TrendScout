"""Stable fingerprints for source evidence used by Collections snapshots."""

from __future__ import annotations

import hashlib
import json

_V1_TRANSIENT_COVERAGE_FIELDS = {
    "delivery_state",
    "provider_request_count",
    "cached_provider_request_count",
    "refresh_provider_request_count",
    "attempted_calls",
    "serp_verification_attempted",
    "cached_serp_verification_attempted",
    "serp_verification_failed_count",
    "serp_admission_blocked_count",
    "cache_delivery_at",
    "cache_fetched_at",
    "cache_freshness",
    "cache_state",
    "cache_status",
    "cache_reason",
    "shared_cache_state",
    "shared_cache_fetched_at",
    "next_eligible_at",
    "refresh_eligible",
    "refresh_attempt_status",
    "refresh_attempted_at",
    "refresh_error_code",
    "owner_fresh_acceptance",
}
_TRANSIENT_COVERAGE_FIELDS = _V1_TRANSIENT_COVERAGE_FIELDS | {
    # Refresh scheduling and delivery diagnostics do not describe the records
    # that Collections processes.
    "refresh_state",
    "eligible_slot",
    "next_slots_timezone",
    "refresh_eligibility_state",
    "refresh_reason",
    "refresh_status",
}
PRIVATE_SOURCE_DIGEST_FIELDS = frozenset({
    "source_input_digest",
    "source_input_digest_version",
    "legacy_source_input_digest",
    "legacy_source_input_digests",
})
_TRANSIENT_DELIVERY_QUALITY_FLAGS = frozenset({
    "STALE_SHARED_CACHE_REUSED",
    "STALE_LOCAL_CACHE_REUSED",
})
# Bump this when a Collections processing change makes identical source
# evidence require a new dataset build.
COLLECTION_INPUT_DIGEST_VERSION = 2


def _valid_digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _stable_value(value):
    if isinstance(value, dict):
        return {
            key: _stable_value(child)
            for key, child in value.items()
            if key != "delivery_state"
        }
    if isinstance(value, list):
        return [_stable_value(child) for child in value]
    return value


def _stable_record_value(value):
    """Keep all normalized record fields except delivery labels added by cache reads."""
    if isinstance(value, dict):
        stable = {}
        for key, child in value.items():
            if key == "delivery_state":
                continue
            if key == "quality_flags":
                flags = _stable_quality_flags(child)
                if isinstance(child, list) and not flags:
                    continue
                stable[key] = flags
            else:
                stable[key] = _stable_record_value(child)
        return stable
    if isinstance(value, list):
        return [_stable_record_value(child) for child in value]
    return value


def _stable_quality_flags(value):
    if not isinstance(value, list):
        return value
    return [flag for flag in value
            if not isinstance(flag, str)
            or flag not in _TRANSIENT_DELIVERY_QUALITY_FLAGS]


def _stable_coverage(value):
    return {
        key: _stable_value(child)
        for key, child in value.items()
        if key not in _TRANSIENT_COVERAGE_FIELDS
    }


def _stable_coverage_v1(value):
    return {
        key: _stable_value(child)
        for key, child in value.items()
        if key not in _V1_TRANSIENT_COVERAGE_FIELDS
    }


def _legacy_business_metadata_matches(source, previous_manifest_item):
    records = source.get("records")
    current_coverage = source.get("coverage")
    previous_coverage = previous_manifest_item.get("coverage")
    current_count = source.get("record_count")
    previous_count = previous_manifest_item.get("record_count")
    return (
        source.get("data_mode") == previous_manifest_item.get("data_mode")
        and type(current_count) is int
        and current_count == previous_count
        and isinstance(records, list)
        and len(records) == current_count
        and _stable_quality_flags(source.get("quality_flags", []))
        == _stable_quality_flags(previous_manifest_item.get("quality_flags", []))
        and isinstance(current_coverage, dict)
        and isinstance(previous_coverage, dict)
        and _stable_coverage(current_coverage) == _stable_coverage(previous_coverage)
    )


def _reports_zero_provider_requests(source):
    if (type(source.get("provider_request_count")) is not int
            or source["provider_request_count"] != 0):
        return False
    coverage = source.get("coverage")
    if not isinstance(coverage, dict) or not coverage:
        return False
    for field in (
        "provider_request_count",
        "refresh_provider_request_count",
        "serp_verification_attempted",
    ):
        count = coverage.get(field)
        if count is not None and (type(count) is not int or count != 0):
            return False
    return True


def source_input_digest(source_id: str, source: dict) -> str:
    """Hash source evidence that can change the business Collections dataset.

    Delivery freshness, refresh permission, and operational status stay in the
    run receipt. They do not change the evidence used by Collections when the
    normalized records, scope, count, and quality flags are unchanged.
    """
    if not isinstance(source_id, str) or not source_id or not isinstance(source, dict):
        raise ValueError("source identity and result are required")
    records = source.get("records", [])
    if not isinstance(records, list):
        raise ValueError("source records must be a list")
    coverage = source.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    payload = {
        "digest_version": COLLECTION_INPUT_DIGEST_VERSION,
        "source_id": source_id,
        "data_mode": source.get("data_mode"),
        "record_count": source.get("record_count"),
        "quality_flags": _stable_quality_flags(source.get("quality_flags", [])),
        "coverage": _stable_coverage(coverage),
        "records": _stable_record_value(records),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def legacy_source_input_digest(source_id: str, source: dict, previous_manifest_item: dict):
    """Rebuild a v1 digest with the saved delivery metadata and current records.

    Older dataset manifests hashed status and refresh state together with the
    records. Supplying the saved metadata lets a new Fetch prove the records
    match that legacy digest without trusting cache timestamps or doing a one-off
    dataset rebuild.
    """
    if (not isinstance(source_id, str) or not source_id
            or not isinstance(source, dict)
            or not isinstance(previous_manifest_item, dict)):
        return None
    if not _legacy_business_metadata_matches(source, previous_manifest_item):
        return None
    records = source.get("records")
    coverage = previous_manifest_item.get("coverage")
    if not isinstance(records, list) or not isinstance(coverage, dict):
        return None
    payload = {
        "digest_version": 1,
        "source_id": source_id,
        "status": previous_manifest_item.get("status"),
        "data_mode": previous_manifest_item.get("data_mode"),
        "record_count": previous_manifest_item.get("record_count"),
        "quality_flags": previous_manifest_item.get("quality_flags", []),
        "error_code": previous_manifest_item.get("error_code"),
        "retry_after_seconds": previous_manifest_item.get("retry_after_seconds"),
        "coverage": _stable_coverage_v1(coverage),
        "records": _stable_value(records),
    }
    try:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def legacy_source_input_digest_candidates(source_id: str, source: dict,
                                          previous_manifest_item: dict):
    """Return exact v1 and delivery-normalized v1 fingerprints for upgrade reuse.

    V1 digests included cache-added stale warning flags inside records. The
    normalized candidate lets an old cached dataset match when only those
    delivery warnings changed; the exact candidate preserves compatibility
    with datasets that were originally built from stale cache evidence.
    """
    exact = legacy_source_input_digest(source_id, source, previous_manifest_item)
    if exact is None:
        return []
    records = source.get("records") if isinstance(source, dict) else None
    if not isinstance(records, list):
        return [exact]
    coverage = previous_manifest_item.get("coverage")
    if not isinstance(coverage, dict):
        return [exact]
    payload = {
        "digest_version": 1,
        "source_id": source_id,
        "status": previous_manifest_item.get("status"),
        "data_mode": previous_manifest_item.get("data_mode"),
        "record_count": previous_manifest_item.get("record_count"),
        "quality_flags": previous_manifest_item.get("quality_flags", []),
        "error_code": previous_manifest_item.get("error_code"),
        "retry_after_seconds": previous_manifest_item.get("retry_after_seconds"),
        "coverage": _stable_coverage_v1(coverage),
        "records": _stable_record_value(records),
    }
    try:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return [exact]
    normalized = hashlib.sha256(encoded).hexdigest()
    return [exact] if normalized == exact else [exact, normalized]


def reusable_collection_input(current_sources, previous_manifest, *, processing_status):
    """Return whether a cache-only result exactly matches a saved dataset input."""
    if (processing_status != "COMPLETED"
            or not isinstance(current_sources, list) or not current_sources
            or not isinstance(previous_manifest, list)):
        return False
    current = []
    for source in current_sources:
        if (not isinstance(source, dict)
                or source.get("status") not in {"SUCCESS", "PARTIAL"}
                or source.get("data_mode") != "LIVE"
                or source.get("delivery_state") not in {"CACHED", "STALE_CACHE"}
                or not _reports_zero_provider_requests(source)
                or not isinstance(source.get("records"), list)
                or type(source.get("record_count")) is not int
                or source["record_count"] != len(source["records"])):
            return False
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            return False
        try:
            digest = source_input_digest(source_id, source)
        except (TypeError, ValueError, UnicodeError, OverflowError):
            return False
        previous_item = next((item for item in previous_manifest
                              if isinstance(item, dict)
                              and item.get("source_id") == source_id), {})
        legacy_digests = legacy_source_input_digest_candidates(
            source_id, source, previous_item,
        )
        current.append({
            "source_id": source_id,
            "status": source["status"],
            "data_mode": "LIVE",
            "delivery_state": source["delivery_state"],
            "provider_request_count": 0,
            "coverage": source.get("coverage"),
            "source_input_digest": digest,
            "source_input_digest_version": COLLECTION_INPUT_DIGEST_VERSION,
            "legacy_source_input_digest": legacy_source_input_digest(
                source_id, source, previous_item,
            ),
            "legacy_source_input_digests": legacy_digests,
        })
    return reusable_collection_manifest(
        current, previous_manifest, processing_status=processing_status,
    )


def reusable_collection_manifest(current_manifest, previous_manifest, *, processing_status):
    """Match compact Fetch history manifests against a saved dataset manifest."""
    if (processing_status != "COMPLETED"
            or not isinstance(current_manifest, list) or not current_manifest
            or not isinstance(previous_manifest, list)):
        return False
    previous = {}
    for item in previous_manifest:
        if (not isinstance(item, dict)
                or not isinstance(item.get("source_id"), str)
                or not item.get("source_id")
                or item["source_id"] in previous
                or not _valid_digest(item.get("source_input_digest"))):
            return False
        previous[item["source_id"]] = item
    current_ids = set()
    for source in current_manifest:
        if (not isinstance(source, dict)
                or source.get("status") not in {"SUCCESS", "PARTIAL"}
                or source.get("data_mode") != "LIVE"
                or source.get("delivery_state") not in {"CACHED", "STALE_CACHE"}
                or not _reports_zero_provider_requests(source)
                or not _valid_digest(source.get("source_input_digest"))):
            return False
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in current_ids:
            return False
        current_ids.add(source_id)
        saved = previous.get(source_id)
        if not isinstance(saved, dict):
            return False
        saved_version = saved.get("source_input_digest_version")
        current_version = source.get("source_input_digest_version")
        if (saved_version == current_version == COLLECTION_INPUT_DIGEST_VERSION
                and saved["source_input_digest"] == source.get("source_input_digest")):
            continue
        if saved_version is None and _valid_digest(saved.get("source_input_digest")):
            candidates = source.get("legacy_source_input_digests")
            if not isinstance(candidates, list):
                candidates = [source.get("legacy_source_input_digest")]
            if saved.get("source_input_digest") in candidates:
                continue
        if (saved_version is None and current_version is None
                and _valid_digest(saved.get("source_input_digest"))
                and saved["source_input_digest"] == source.get("source_input_digest")):
            continue
        return False
    return current_ids == set(previous)
