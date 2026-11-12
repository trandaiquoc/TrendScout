"""Run the owner-authorized one-request D6.2 Wikimedia local LIVE smoke."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import socket
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import uvicorn

from app import local_ledger
from app.jobs.source_checkpoints import CheckpointGrant
from app.main import create_local_live_app
from app.orchestration.local_live_composition import local_live_composition
from app.settings import Settings
from app.local_smoke_policy import local_smoke_policy
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
    source_capability,
)
from app.sources.live_gateway import SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN

_CONTACT = re.compile(
    r"\([^\r\n()]*?(?:https?://|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})[^\r\n()]*\)"
)
_OTHER_SOURCES = frozenset({GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID})


class WikimediaOnlyApprovals:
    """One task-scoped source approval; all other live providers fail closed."""

    def __init__(self, expires_at: datetime):
        self.expires_at = expires_at

    def current(self, source_id: str):
        if source_id != WIKIMEDIA_SOURCE_ID:
            return None
        return SourceApproval(
            source_id=source_id,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            expires_at=self.expires_at,
            policy_version="d6.2-wikimedia-one-request-v1",
            approval_id="D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12",
            source_enabled=True,
            global_enabled=True,
            usage_approved=True,
            retention_approved=True,
            quota_approved=True,
            cost_approved=True,
            security_approved=True,
            egress_controls_verified=True,
            telemetry_controls_verified=True,
            scope="LOCAL_SMOKE",
            local_controls_verified=True,
        )


class NoProviderCredentials:
    def resolve(self, reference):
        raise RuntimeError(f"unexpected credential resolution: {reference}")


def wikimedia_reservation_state(database: Path) -> str:
    """Read the canonical quota ledger without changing it or exposing its contents."""
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
            return "RESERVED"
        for job in jobs.values():
            if not isinstance(job, dict) or not isinstance(job.get("receipts", {}), dict):
                return "INVALID_LEDGER"
            if any(receipt.get("source_id") == WIKIMEDIA_SOURCE_ID
                   for receipt in job.get("receipts", {}).values()
                   if isinstance(receipt, dict)):
                return "RESERVED"
        return "NO_RESERVATION"
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return "INVALID_LEDGER"


def _summary(status_body):
    if not isinstance(status_body, dict):
        return None
    sources = []
    for row in status_body.get("sources", []):
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
    started_at = datetime.now(timezone.utc)
    observation_date = (started_at - timedelta(days=2)).date()
    expiry = started_at + timedelta(minutes=5)
    approvals = WikimediaOnlyApprovals(expiry)
    user_agent_override = os.environ.get("WIKIMEDIA_USER_AGENT")
    settings_env = {"TREND_SCOUT_ENVIRONMENT": "local"}
    if user_agent_override is not None:
        settings_env["WIKIMEDIA_USER_AGENT"] = user_agent_override
    settings = Settings.from_environment(settings_env)
    if not _CONTACT.search(settings.wikimedia_user_agent):
        return {"status": "BLOCKED", "reason": "USER_AGENT_CONTACT_INVALID"}
    if approvals.current(WIKIMEDIA_SOURCE_ID) is None or any(
        approvals.current(source_id) is not None for source_id in _OTHER_SOURCES
    ):
        return {"status": "BLOCKED", "reason": "SOURCE_ISOLATION_INVALID"}

    capability = source_capability(WIKIMEDIA_SOURCE_ID)
    endpoint = capability.endpoint + f"/{observation_date:%Y/%m/%d}"
    expected_path = urlsplit(endpoint).path
    live_request_count = 0
    live_http_status = None
    unexpected_hosts = []
    database = local_ledger.DIRECTORY / "ledger.sqlite"
    ledger_was_absent = not database.exists()
    original_send = httpx.AsyncHTTPTransport.handle_async_request

    async def counted_send(transport, request):
        nonlocal live_request_count, live_http_status
        host = request.url.host
        if host == "wikimedia.org":
            if (request.method != "GET" or request.url.scheme != "https"
                    or request.url.path != expected_path or request.url.query):
                raise RuntimeError("Wikimedia outbound request exceeded the approved scope")
            live_request_count += 1
            response = await original_send(transport, request)
            live_http_status = response.status_code
            return response
        if host not in {"127.0.0.1", "localhost"}:
            unexpected_hosts.append(host)
            raise RuntimeError("unexpected outbound host")
        return await original_send(transport, request)

    fixed_clock = lambda: started_at

    def checkpoint_grant(source_id, now):
        return CheckpointGrant(
            source_id, "LIVE", "d6.2-wikimedia-volatile-120s",
            now + timedelta(minutes=5), 120, True,
        )

    def composition_factory():
        return local_live_composition(
            credentials=NoProviderCredentials(),
            approvals=approvals,
            user_agent=settings.wikimedia_user_agent,
            resolve_checkpoint_grant=checkpoint_grant,
            resolve_processing_grants=lambda _now: (),
            utcnow=fixed_clock,
        )

    application = create_local_live_app(
        enabled=True, workers=1, composition_factory=composition_factory, settings=settings,
    )
    output = {
        "status": "BLOCKED",
        "preflight": {
            "utc_observation_date": observation_date.isoformat(),
            "source_id": WIKIMEDIA_SOURCE_ID,
            "endpoint": endpoint,
            "user_agent_contact_valid": True,
            "approval_id": "D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12",
            "approval_valid": True,
            "processing_grant": "ABSENT",
            "other_sources_approved": False,
            "initial_ledger_file": "ABSENT" if ledger_was_absent else "PRESENT",
        },
        "provider_request_count": 0,
        "http_status": None,
    }

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = uvicorn.Config(
        application, host="127.0.0.1", port=port, workers=1,
        proxy_headers=False, access_log=False, log_level="critical",
    )
    server = uvicorn.Server(config)
    server_task = None
    try:
        with patch_transport(counted_send):
            server_task = asyncio.create_task(server.serve())
            for _ in range(400):
                if server.started:
                    break
                if server_task.done():
                    return output
                await asyncio.sleep(0.025)
            if not server.started:
                return output

            state = wikimedia_reservation_state(database)
            output["preflight"]["quota_reservation_state"] = state
            if state not in {"NO_LEDGER", "NO_RESERVATION"}:
                output["reason"] = "WIKIMEDIA_QUOTA_ALREADY_RESERVED"
                return output

            active_policy = local_smoke_policy()
            wiki_limit = active_policy.pools["wikimedia"]
            if (wiki_limit.daily_requests != 1 or wiki_limit.monthly_requests != 1
                    or wiki_limit.daily_cost_units != 1 or wiki_limit.monthly_cost_units != 1
                    or active_policy.max_starts_per_window != 1
                    or active_policy.window_seconds != 86_400):
                output["reason"] = "LOCAL_QUOTA_POLICY_MISMATCH"
                return output
            output["preflight"]["local_quota_policy"] = "ONE_REQUEST_ONE_ATTEMPT_24H"
            output["preflight"]["source_isolation"] = "WIKIMEDIA_ONLY"
            output["preflight"]["processing_grant"] = "ABSENT"
            print(json.dumps({"preflight": output["preflight"]}, sort_keys=True), flush=True)

            timeout = httpx.Timeout(25.0, connect=5.0)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=timeout,
                follow_redirects=False, trust_env=False,
            ) as client:
                key = f"d6-2-wikimedia-{uuid4().hex}"
                accepted = await client.post(
                    "/api/v1/fetches", json={}, headers={"Idempotency-Key": key},
                )
                output["local_post_status"] = accepted.status_code
                if accepted.status_code != 202:
                    output["reason"] = "LOCAL_FETCH_NOT_ACCEPTED"
                    return output
                accepted_body = accepted.json()
                status_url = accepted_body.get("status_url")
                if not isinstance(status_url, str) or not status_url.startswith("/api/v1/fetches/"):
                    output["reason"] = "LOCAL_STATUS_URL_INVALID"
                    return output
                status_response = None
                for attempt in range(10):
                    status_response = await client.get(status_url)
                    if status_response.status_code != 200:
                        break
                    status_body = status_response.json()
                    if status_body.get("terminal") is True:
                        break
                    if attempt < 9:
                        await asyncio.sleep(0.25)
                if status_response is None or status_response.status_code != 200:
                    output["reason"] = "LOCAL_STATUS_FAILED"
                    return output
                output["local_status_http_status"] = status_response.status_code
                output["fetch"] = _summary(status_response.json())
                output["provider_request_count"] = live_request_count
                output["http_status"] = live_http_status
                output["unexpected_hosts"] = unexpected_hosts
                output["quota_reservation_state_after"] = wikimedia_reservation_state(database)
                wiki_row = next((row for row in output["fetch"]["sources"]
                                 if row["source_id"] == WIKIMEDIA_SOURCE_ID), {})
                success = (
                    live_request_count == 1 and live_http_status == 200
                    and not unexpected_hosts
                    and wiki_row.get("status") == "SUCCESS"
                    and wiki_row.get("data_mode") == "LIVE"
                    and type(wiki_row.get("record_count")) is int
                    and wiki_row["record_count"] > 0
                    and output["quota_reservation_state_after"] == "RESERVED"
                    and output["fetch"].get("discovery_candidate_count") == 0
                )
                output["status"] = "WIKIMEDIA_LIVE_SMOKE_VERIFIED" if success else "PARTIAL"
                output["local_status_http_status"] = status_response.status_code
                return output
    except Exception as exc:
        output["status"] = "FAILED_OR_BLOCKED"
        output["reason"] = "RUNTIME_ERROR"
        output["error_type"] = type(exc).__name__
        output["provider_request_count"] = live_request_count
        output["http_status"] = live_http_status
        output["unexpected_hosts"] = unexpected_hosts
        if database.exists():
            output["quota_reservation_state_after"] = wikimedia_reservation_state(database)
        return output
    finally:
        if server_task is not None and not server_task.done():
            server.should_exit = True
            try:
                await asyncio.wait_for(server_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                server_task.cancel()


def patch_transport(handler):
    """Patch request dispatch for this single-process activation run."""
    from unittest.mock import patch

    return patch.object(httpx.AsyncHTTPTransport, "handle_async_request", handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="One-request Wikimedia-only D6.2 LIVE smoke")
    parser.add_argument("--owner-approved-wikimedia-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.owner_approved_wikimedia_only:
        print('{"status":"BLOCKED","reason":"EXPLICIT_D6_2_OPT_IN_REQUIRED"}')
        return 2
    logging.disable(logging.CRITICAL)
    try:
        result = asyncio.run(_run_once())
    except Exception:
        result = {
            "status": "FAILED_OR_BLOCKED",
            "reason": "PRE_REQUEST_RUNTIME_EXCEPTION",
            "provider_request_count": 0,
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "WIKIMEDIA_LIVE_SMOKE_VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
