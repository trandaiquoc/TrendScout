"""Read-only preflight for the existing D6 Firestore admission ledger.

This prints and saves only policy digests and aggregate ledger counts. It never
prints job IDs, quota values, cache contents, credentials, or provider data.
The Firestore transaction returns the original state unchanged, so the storage
adapter does not issue a Firestore write.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.google_live_settings import GoogleLiveSettings
from app.jobs.public_admission import PublicAdmission
from app.normal_wikimedia_live_policy import GOOGLE_STATE_KEY, normal_google_live_policy


def _policies_for_preflight(settings: GoogleLiveSettings):
    common = {
        "serpapi_daily_budget": settings.serpapi_daily_budget,
        "serpapi_monthly_budget": settings.serpapi_monthly_budget,
        "rss_daily_budget": settings.rss_daily_budget,
        "rss_monthly_budget": settings.rss_monthly_budget,
        "fetch_starts_per_hour": settings.fetch_starts_per_hour,
        "bing_daily_budget": settings.bing_daily_budget,
    }
    target_bing = settings.paid_bing_approved
    return {
        "windows2_current": normal_google_live_policy(
            **common, bing_enabled=target_bing, youtube_enabled=True
        ),
        "previous_without_bing": normal_google_live_policy(
            **common, bing_enabled=False, youtube_enabled=True
        ),
        "previous_without_youtube": normal_google_live_policy(
            **common, bing_enabled=target_bing, youtube_enabled=False
        ),
        "previous_without_bing_or_youtube": normal_google_live_policy(
            **common, bing_enabled=False, youtube_enabled=False
        ),
        "previous_capacity_one_without_bing_or_youtube": normal_google_live_policy(
            **{**common, "fetch_starts_per_hour": 1},
            bing_enabled=False,
            youtube_enabled=False,
        ),
        "legacy_low_budget_without_bing_or_youtube": normal_google_live_policy(
            serpapi_daily_budget=10,
            serpapi_monthly_budget=10,
            rss_daily_budget=1,
            rss_monthly_budget=1,
            fetch_starts_per_hour=2,
            bing_enabled=False,
            bing_daily_budget=settings.bing_daily_budget,
            youtube_enabled=False,
        ),
    }


def summarize_ledger(state: dict, policies: dict[str, object]) -> dict:
    """Return a sanitized comparison; never include ledger values or IDs."""
    if not isinstance(state, dict):
        raise ValueError("Firestore admission state must be a mapping")
    stored_digest = state.get("policy_digest")
    if not isinstance(stored_digest, str) or len(stored_digest) != 64:
        stored_digest = None
    digests = {
        label: PublicAdmission._policy_digest(policy)
        for label, policy in policies.items()
    }
    matches = sorted(label for label, digest in digests.items()
                     if stored_digest is not None and digest == stored_digest)
    jobs = state.get("jobs")
    keys = state.get("keys")
    starts = state.get("starts")
    quota = state.get("quota")
    inflight = state.get("inflight")
    audit = state.get("policy_migration_audit")
    return {
        "project": "trendscout-192e4",
        "database": "(default)",
        "state_key": GOOGLE_STATE_KEY,
        "state_present": bool(state),
        "schema": state.get("schema"),
        "stored_policy_digest": stored_digest,
        "windows2_current_policy_digest": digests["windows2_current"],
        "matches_windows2_current_policy": (
            stored_digest == digests["windows2_current"]
        ),
        "recognized_previous_policy_labels": matches,
        "operational_enabled": (
            state.get("operational_enabled")
            if type(state.get("operational_enabled")) is bool else None
        ),
        "jobs_count": len(jobs) if isinstance(jobs, dict) else None,
        "idempotency_keys_count": len(keys) if isinstance(keys, dict) else None,
        "starts_count": len(starts) if isinstance(starts, list) else None,
        "quota_counter_count": len(quota) if isinstance(quota, dict) else None,
        "inflight_count": len(inflight) if isinstance(inflight, dict) else None,
        "migration_audit_count": len(audit) if isinstance(audit, list) else None,
        "read_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def classify_failure(error: BaseException) -> str:
    """Map authentication/read failures to a safe, non-sensitive reason code."""
    parts = []
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current).casefold())
        current = current.__cause__ or current.__context__
    detail = " ".join(parts)
    if ("iam.serviceaccounts.getaccesstoken" in detail
            and ("denied" in detail or "permission_denied" in detail)):
        return "SERVICE_ACCOUNT_IMPERSONATION_DENIED"
    return "FIRESTORE_PREFLIGHT_FAILED"


def failure_report(*, stage: str, code: str) -> dict:
    """Describe a failed read without copying provider, ledger, or credential data."""
    if stage not in {"ADC_IMPERSONATION", "FIRESTORE_READ"}:
        raise ValueError("invalid preflight failure stage")
    if code not in {
        "SERVICE_ACCOUNT_IMPERSONATION_DENIED",
        "FIRESTORE_PREFLIGHT_FAILED",
    }:
        raise ValueError("invalid preflight failure code")
    return {
        "project": "trendscout-192e4",
        "database": "(default)",
        "state_key": GOOGLE_STATE_KEY,
        "result": "FAILED",
        "failure_stage": stage,
        "failure_code": code,
        "firestore_read_attempted": stage == "FIRESTORE_READ",
        "firestore_state_read": False if stage == "ADC_IMPERSONATION" else None,
        "firestore_state_write": False,
        "provider_calls": 0,
        "read_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _save_report(report: dict) -> Path:
    output_path = (
        Path(os.environ.get("TEMP") or tempfile.gettempdir())
        / "trendscout-d6-firestore-policy-preflight.json"
    )
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return output_path


def main():
    if os.environ.get("VERCEL"):
        raise SystemExit("Run this read-only preflight on Windows 2, not Vercel.")

    from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
    from scripts.run_google_live_local import (
        _create_firestore_shared_live_client,
        local_live_settings,
    )

    client = None
    try:
        # Resolve impersonation now so a missing getAccessToken permission is
        # reported immediately instead of retried by Firestore as UNAVAILABLE.
        client = _create_firestore_shared_live_client(refresh_credentials=True)
        settings = local_live_settings()
        policies = _policies_for_preflight(settings)
        store = FirestoreAtomicStateStore(client)
        state = store.transact(GOOGLE_STATE_KEY, lambda current: (current, current))
        report = summarize_ledger(state, policies)
    except Exception as exc:
        report = failure_report(
            stage="ADC_IMPERSONATION" if client is None else "FIRESTORE_READ",
            code=classify_failure(exc),
        )
        output_path = _save_report(report)
        print(json.dumps(report, indent=2, sort_keys=True))
        print(f"Sanitized report saved to: {output_path}")
        return 1
    finally:
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    output_path = _save_report(report)
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"Sanitized report saved to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
