"""Fail-closed retirement shim for the historical D6.2 LIVE launcher."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from app import local_ledger
from app.sources.catalog import WIKIMEDIA_SOURCE_ID

LEGACY_APPROVAL_ID = "D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12"
LEGACY_APPROVAL_BLOCKED = "LEGACY_APPROVAL_EXPIRED_OR_CONSUMED"

_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DIAGNOSTIC_FIELDS = frozenset({
    "source_id", "failure_phase", "http_status", "exception_class",
    "bytes_received", "response_size_limit", "attempt_id", "error_code",
})
_DIAGNOSTIC_PHASES = frozenset({
    "REQUEST_DISPATCH", "RESPONSE_HEADERS", "BODY_STREAM", "JSON_DECODE",
    "SCHEMA_VALIDATION",
})
_DIAGNOSTIC_EXCEPTIONS = frozenset({
    "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout", "RemoteProtocolError",
    "DecodingError", "WriteError", "WriteTimeout", "PoolTimeout", "CloseError",
    "RequestError", "TimeoutException", "HttpDeadlineExceeded", "TimeoutError",
    "JSONDecodeError", "ValueError", "TypeError", "RecursionError",
})


class WikimediaOnlyApprovals:
    """Compatibility shim: the historical approval is permanently retired."""

    def __init__(self, expires_at=None):
        # Kept only to make attempted expiry refreshes explicit and harmless.
        self.requested_expiry = expires_at

    def current(self, source_id: str):
        return None


class NoProviderCredentials:
    """Compatibility guard that never resolves provider credentials."""

    def resolve(self, reference):
        raise RuntimeError("provider credential resolution is disabled in the retired launcher")


def legacy_approval_preflight(*, approval_id=LEGACY_APPROVAL_ID, expires_at=None) -> dict:
    """Reject the retired identifier regardless of caller, clock, machine, or ledger."""
    return {
        "status": "BLOCKED",
        "reason": LEGACY_APPROVAL_BLOCKED,
        "approval_id": approval_id,
        "approval_retired": approval_id == LEGACY_APPROVAL_ID,
        "provider_request_count": 0,
    }


def wikimedia_reservation_state(database: Path) -> str:
    """Read one ledger path without mutation; absence is never authorization."""
    if not database.exists():
        return "NO_LEDGER"
    uri = database.as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
            row = connection.execute(
                "SELECT state_json FROM atomic_state WHERE state_key = ?",
                (local_ledger.STATE_KEY,),
            ).fetchone()
        if row is None:
            return "NO_RESERVATION"
        state = json.loads(row[0])
        if not isinstance(state, dict):
            return "INVALID_LEDGER"
        counts = state.get("local_smoke_source_counts", {})
        jobs = state.get("jobs", {})
        if not isinstance(counts, dict) or not isinstance(jobs, dict):
            return "INVALID_LEDGER"
        if WIKIMEDIA_SOURCE_ID in counts:
            count = counts[WIKIMEDIA_SOURCE_ID]
            if type(count) is not int or count < 1:
                return "INVALID_LEDGER"
            return "RESERVED"
        for job in jobs.values():
            if not isinstance(job, dict):
                return "INVALID_LEDGER"
            receipts = job.get("receipts", {})
            if not isinstance(receipts, dict):
                return "INVALID_LEDGER"
            for receipt in receipts.values():
                if not isinstance(receipt, dict):
                    return "INVALID_LEDGER"
                if receipt.get("source_id") == WIKIMEDIA_SOURCE_ID:
                    return "RESERVED"
        return "NO_RESERVATION"
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return "INVALID_LEDGER"


def _valid_transport_diagnostic(value, *, source_error_code=None) -> bool:
    if not isinstance(value, dict) or set(value) != _DIAGNOSTIC_FIELDS:
        return False
    return not (
        value["source_id"] != WIKIMEDIA_SOURCE_ID
        or not isinstance(value["failure_phase"], str)
        or value["failure_phase"] not in _DIAGNOSTIC_PHASES
        or (value["http_status"] is not None and (
            type(value["http_status"]) is not int or not 100 <= value["http_status"] <= 599
        ))
        or (value["exception_class"] is not None and (
            not isinstance(value["exception_class"], str)
            or value["exception_class"] not in _DIAGNOSTIC_EXCEPTIONS
        ))
        or type(value["bytes_received"]) is not int
        or not 0 <= value["bytes_received"] <= 1_000_000_000
        or type(value["response_size_limit"]) is not int
        or not 1 <= value["response_size_limit"] <= 10_000_000
        or not isinstance(value["attempt_id"], str)
        or not _SAFE_ATTEMPT_ID.fullmatch(value["attempt_id"])
        or not isinstance(value["error_code"], str)
        or not _SAFE_CODE.fullmatch(value["error_code"])
        or value["error_code"] != source_error_code
    )


def _summary(status_body):
    """Select status fields and validated diagnostics; omit malformed diagnostics."""
    if not isinstance(status_body, dict):
        return None
    raw_sources = status_body.get("sources", [])
    sources = []
    if isinstance(raw_sources, list):
        for row in raw_sources:
            if not isinstance(row, dict):
                continue
            source = {
                "source_id": row.get("source_id"),
                "status": row.get("status"),
                "data_mode": row.get("data_mode"),
                "record_count": row.get("record_count", 0),
                "error_code": row.get("error_code"),
            }
            coverage = row.get("coverage")
            if isinstance(coverage, dict):
                source["rejected_count"] = coverage.get("rejected", 0)
                source["observation_date"] = coverage.get("observation_date")
            diagnostic = row.get("transport_diagnostic")
            if _valid_transport_diagnostic(diagnostic, source_error_code=row.get("error_code")):
                source["transport_diagnostic"] = {
                    field: diagnostic[field] for field in (
                        "source_id", "failure_phase", "http_status", "exception_class",
                        "bytes_received", "response_size_limit", "attempt_id", "error_code",
                    )
                }
            sources.append(source)
    discovery = status_body.get("discovery")
    return {
        "fetch_status": status_body.get("status"),
        "data_mode": status_body.get("data_mode"),
        "sources": sources,
        "discovery_status": discovery.get("status") if isinstance(discovery, dict) else None,
        "discovery_candidate_count": (
            discovery.get("candidate_count", 0) if isinstance(discovery, dict) else 0
        ),
        "discovery_limitations": (
            discovery.get("limitations", []) if isinstance(discovery, dict) else []
        ),
    }


async def _run_once():
    """Legacy activation entrypoint; always blocks before app or transport creation."""
    return legacy_approval_preflight()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Retired D6.2 Wikimedia LIVE launcher")
    parser.add_argument("--owner-approved-wikimedia-only", action="store_true")
    parser.parse_args(argv)
    print(json.dumps(legacy_approval_preflight(), sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
