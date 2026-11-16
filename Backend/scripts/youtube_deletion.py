"""Local operator workflow for verified YouTube API data deletion requests."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.jobs.shared_cache import CacheScope
from app.normal_wikimedia_live_policy import GOOGLE_POLICY_VERSION, GOOGLE_STATE_KEY
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.youtube.deletion import (
    LocalYouTubeDeletionWorkflow,
    YouTubeDeletionError,
)
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore


def _paths(state_override=None, collections_override=None):
    home = Path.home()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is required for the Windows deletion workflow")
    state_database = (Path(state_override) if state_override else
                      home / ".local" / "state" / "trendscout" / GOOGLE_STATE_KEY / "state.sqlite")
    collections_database = (Path(collections_override) if collections_override else
                            Path(local_app_data) / "TrendScout" / "collections-v1" / "collections.sqlite")
    if not state_database.is_file():
        raise RuntimeError("the existing TrendScout state database was not found; no database was created")
    if state_override and collections_override is None:
        collections_database = None
    elif collections_database is not None and not collections_database.is_file():
        collections_database = None
    return state_database, collections_database


def _workflow(state_override=None, collections_override=None):
    state_database, collections_database = _paths(state_override, collections_override)
    store = LocalSQLiteAtomicStateStore.for_local_live(state_database)
    scope = CacheScope(
        source_id="youtube_video_search",
        provider_id="youtube_data_api_v3",
        query=DEFAULT_QUERY_PLAN.fingerprint,
        plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        market=DEFAULT_QUERY_PLAN.market,
        language=DEFAULT_QUERY_PLAN.language,
        window=DEFAULT_QUERY_PLAN.time_filter_rule,
        parser_version="source-v1",
        policy_version=GOOGLE_POLICY_VERSION,
    )
    return LocalYouTubeDeletionWorkflow(
        store, cache_scope=scope, collection_database=collections_database
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    receive = commands.add_parser("receive", help="record an operator-received request and issue an opaque ticket")
    preview = commands.add_parser("preview", help="show local YouTube-only deletion counts without changes")
    fulfill = commands.add_parser("fulfill", help="fulfill a manually verified request")
    fulfill.add_argument("--request-ref", required=True)
    for command in (receive, preview, fulfill):
        command.add_argument("--state-db", help="explicit private TrendScout state database or backup copy")
        command.add_argument("--collections-db", help="explicit private Collections database or backup copy")
    args = parser.parse_args(argv)

    try:
        workflow = _workflow(args.state_db, args.collections_db)
        now = datetime.now(timezone.utc)
        if args.command == "receive":
            reference = workflow.receive_request(now=now)
            print("Opaque request reference (shown once; do not add requester details):")
            print(reference)
            return 0
        if args.command == "preview":
            print(json.dumps(workflow.preview(now=now), indent=2, sort_keys=True))
            return 0

        verified = input(
            "Confirm you verified the requester through an independently known contact channel; "
            "type VERIFIED: "
        ).strip()
        stopped = input(
            "Confirm all TrendScout processes using this local database are stopped; type STOPPED: "
        ).strip()
        confirmation = input("Type DELETE YOUTUBE DATA to proceed: ").strip()
        if verified != "VERIFIED" or stopped != "STOPPED" or confirmation != "DELETE YOUTUBE DATA":
            print("Deletion cancelled; no data was changed.", file=sys.stderr)
            return 2
        result = workflow.fulfill_request(
            args.request_ref, requester_verified=True, application_stopped=True, now=now
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (PermissionError, ValueError, YouTubeDeletionError, RuntimeError):
        print("Deletion workflow stopped safely; review the local runbook.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
