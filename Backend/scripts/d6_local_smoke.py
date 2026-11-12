"""Proposed four-request local smoke. Disabled unless explicitly invoked.

Run only after the project owner approves provider use, request count and cost.
No provider response bodies or credentials are printed or persisted here.
"""

import argparse
import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.jobs.public_admission import PublicAdmission
from app.local_credentials import LocalCredentialProvider
from app.local_smoke_policy import local_smoke_policy
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.live_gateway import LivePageGateway, SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.serpapi.pagination import build_contexts, initial_offset
from app.sources.wikimedia.live_gateway import WikimediaRequest, WikimediaTopPageGateway
from app.sources.youtube.pagination import build_execution_plan
from app.storage.local_smoke_store import LocalSmokeStore
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.workers.guarded_attempt import GuardedAttemptRunner

_SOURCES = (WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID)
_LEDGER = Path(__file__).resolve().parents[1] / "data" / "local" / "d6-smoke"
_USER_AGENT = "TrendScout/0.1 (https://github.com/trandaiquoc/TrendScout)"
_SAFE_CODES = frozenset({"SOURCE_NOT_APPROVED", "CREDENTIAL_UNAVAILABLE", "RATE_LIMITED",
    "QUOTA_EXHAUSTED", "UPSTREAM_TIMEOUT", "UPSTREAM_ERROR", "DAY_UNAVAILABLE",
    "MALFORMED_RESPONSE", "UNAUTHORIZED", "PERMISSION_DENIED", "PROVIDER_ERROR",
    "ALREADY_RESERVED", "ADMISSION_REVOKED", "LEASE_LOST", "SMOKE_SOURCE_LIMIT",
    "OUTBOUND_DESTINATION_REJECTED", "OUTBOUND_PARAMETERS_REJECTED", "RESPONSE_TOO_LARGE"})


class SmokeApprovals:
    def __init__(self, expires_at):
        self.expires_at = expires_at

    def current(self, source):
        if source not in _SOURCES:
            return None
        return SourceApproval(source, DEFAULT_QUERY_PLAN.fingerprint, self.expires_at,
            "d6-local-smoke-v1", approval_id="owner-approved-local-smoke",
            source_enabled=True, global_enabled=True, usage_approved=True,
            retention_approved=True, quota_approved=True, cost_approved=True,
            security_approved=True, egress_controls_verified=True, telemetry_controls_verified=True,
            scope="LOCAL_SMOKE", local_controls_verified=True)


async def run_smoke():
    credentials = LocalCredentialProvider()
    if any(value != "PRESENT" for value in credentials.status().values()):
        return {"status": "INVALID_CONFIG"}
    _LEDGER.mkdir(parents=True, mode=0o700, exist_ok=True)
    store = LocalSmokeStore(LocalSQLiteAtomicStateStore.for_local_live(_LEDGER / "ledger.sqlite"))
    admission = PublicAdmission(store, replace(local_smoke_policy(), enabled=True), state_key="d6-smoke")
    now = datetime.now(timezone.utc)
    job = admission.admit("d6-one-time-smoke-idempotency", now=now)["job_id"]
    fence = admission.claim(job, "d6-smoke", now=now)["fence"]
    runner = GuardedAttemptRunner(admission)
    approvals = SmokeApprovals(now + timedelta(minutes=5))
    output = {}
    for source in _SOURCES:
        gateway = None
        try:
            admission.heartbeat(job, "d6-smoke", fence, now=datetime.now(timezone.utc))
            args = {"job_id": job, "owner": "d6-smoke", "fence": fence,
                    "attempt_id": "d6-smoke-" + source, "timeout_seconds": 15}
            if source == WIKIMEDIA_SOURCE_ID:
                gateway = await WikimediaTopPageGateway.controlled_network(
                    approvals, runner, user_agent=_USER_AGENT)
                page = await gateway.fetch_day(request=WikimediaRequest(
                    now.date() - timedelta(days=2), DEFAULT_QUERY_PLAN.fingerprint), **args)
                output[source] = {"status": "RESPONSE_VALIDATED", "data_mode": page.data_mode,
                                  "accepted": len(page.observations), "rejected": page.rejected_count}
            else:
                gateway = await LivePageGateway.controlled_network(source, credentials, approvals, runner)
                if source == YOUTUBE_SOURCE_ID:
                    context = build_execution_plan(DEFAULT_QUERY_PLAN, now).queries[0]
                    cursor = None
                else:
                    context = build_contexts(DEFAULT_QUERY_PLAN, source, now)[0]
                    cursor = initial_offset(context)
                page = await gateway.fetch_page(context=context, cursor=cursor, max_results=10, **args)
                output[source] = {"status": "RESPONSE_VALIDATED", "data_mode": page.data_mode,
                                  "items": len(page.page.items)}
        except Exception as exc:
            code = getattr(exc, "code", None)
            code = getattr(code, "value", code)
            output[source] = {"status": "FAILED", "code": code if code in _SAFE_CODES else "SMOKE_ERROR"}
        finally:
            if gateway is not None:
                await gateway.aclose()
    complete = (len(output) == len(_SOURCES)
                and all(row.get("status") == "RESPONSE_VALIDATED"
                        and row.get("data_mode") == "LIVE" for row in output.values()))
    return {"status": "RESPONSES_RECEIVED" if complete else "PARTIAL", "sources": output}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Controlled local provider smoke")
    parser.add_argument("--owner-approved-four-requests", action="store_true")
    parser.add_argument("--confirmed-quota-no-overage", action="store_true")
    parser.add_argument("--confirmed-provider-usage", action="store_true")
    args = parser.parse_args(argv)
    if not (args.owner_approved_four_requests and args.confirmed_quota_no_overage
            and args.confirmed_provider_usage):
        print("BLOCKED: owner approval, provider usage and quota confirmation required")
        return 2
    logging.disable(logging.CRITICAL)
    import json
    try:
        result = asyncio.run(run_smoke())
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") == "RESPONSES_RECEIVED" else 1
    except Exception:
        print('{"status":"BLOCKED"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
