"""Validation shared by the Firestore and in-memory implementations."""

from typing import Any, Mapping

KINDS = frozenset({"snapshots", "observations", "keyword_results", "baseline_buckets"})


def validate_id(value: str) -> None:
    if not isinstance(value, str) or not value or "/" in value or value in {".", ".."} or value.startswith("__") and value.endswith("__") or len(value.encode("utf-8")) > 1500:
        raise ValueError("Invalid Firestore document ID")


def validate_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError("Unknown record kind")


def validate_fields(data: Mapping[str, Any], *, update: bool = False) -> None:
    if not isinstance(data, Mapping) or any(not isinstance(key, str) or not key for key in data):
        raise ValueError("Document must have non-empty string field names")
    if update and (not data or any("." in key for key in data)):
        raise ValueError("Updates require non-empty top-level field names without dots")
