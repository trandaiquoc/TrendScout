"""Stable fingerprints for source evidence used by Collections snapshots."""

from __future__ import annotations

import hashlib
import json

_TRANSIENT_COVERAGE_FIELDS = {
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
# Bump this when a Collections processing change makes identical source
# evidence require a new dataset build.
_COLLECTION_INPUT_DIGEST_VERSION = 1


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


def _stable_coverage(value):
    return {
        key: _stable_value(child)
        for key, child in value.items()
        if key not in _TRANSIENT_COVERAGE_FIELDS
    }


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
    """Hash collection-relevant source evidence, excluding per-fetch delivery state.

    Cache delivery changes only the delivery marker on some normalized records.
    The provider records, their ordering, status, scope, and quality flags remain
    the evidence that determines whether Collections must be recomputed.
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
        "digest_version": _COLLECTION_INPUT_DIGEST_VERSION,
        "source_id": source_id,
        "status": source.get("status"),
        "data_mode": source.get("data_mode"),
        "record_count": source.get("record_count"),
        "quality_flags": source.get("quality_flags", []),
        "error_code": source.get("error_code"),
        "retry_after_seconds": source.get("retry_after_seconds"),
        "coverage": _stable_coverage(coverage),
        "records": _stable_value(records),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
                or source.get("delivery_state") != "CACHED"
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
        current.append({
            "source_id": source_id,
            "status": source["status"],
            "data_mode": "LIVE",
            "delivery_state": "CACHED",
            "provider_request_count": 0,
            "coverage": source.get("coverage"),
            "source_input_digest": digest,
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
                or source.get("delivery_state") != "CACHED"
                or not _reports_zero_provider_requests(source)
                or not _valid_digest(source.get("source_input_digest"))):
            return False
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in current_ids:
            return False
        current_ids.add(source_id)
        saved = previous.get(source_id)
        if (not isinstance(saved, dict)
                or not _valid_digest(saved.get("source_input_digest"))
                or saved["source_input_digest"] != source.get("source_input_digest")):
            return False
    return current_ids == set(previous)
