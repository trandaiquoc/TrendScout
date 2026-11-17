"""Run the local multi-source LIVE web application."""

import argparse
import os
import socket
import sys
from dataclasses import replace
from functools import partial

from app.google_live_settings import GoogleLiveSettings
from app.main import create_normal_google_live_app, run_normal_google_live
from app.orchestration.normal_google_live_composition import normal_google_live_composition
from app.settings import Settings


def local_live_settings():
    """Return the fixed local-web source policy; launch scripts pass no approvals."""
    return replace(
        GoogleLiveSettings.from_environment(),
        trends_approved=True,
        serp_approved=False,
        rss_approved=False,
        bing_approved=True,
        policy_migration_approved=True,
        account_api_approved=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the local multi-source LIVE web application."
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("TREND_SCOUT_DEMO_PORT", "8000")),
    )
    parser.add_argument(
        "--youtube-owner-acceptance", action="store_true",
        help=("Preserve expired SQLite blobs and limit YouTube to one search query; "
              "the regular YouTube schedule and approval checks still apply."),
    )
    parser.add_argument(
        "--youtube-owner-manual-live", action="store_true",
        help=("Enable Linux-only owner-triggered YouTube LIVE development; "
              "normal schedule and internal YouTube caps are bypassed."),
    )
    parser.add_argument(
        "--youtube-owner-force-fresh-test", action="store_true",
        help=("Bypass YouTube cache once for the next selected Fetch; requires "
              "--youtube-owner-manual-live."),
    )
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if arguments.youtube_owner_acceptance and arguments.youtube_owner_manual_live:
        parser.error("choose either --youtube-owner-acceptance or --youtube-owner-manual-live")
    if arguments.youtube_owner_force_fresh_test and not arguments.youtube_owner_manual_live:
        parser.error("--youtube-owner-force-fresh-test requires --youtube-owner-manual-live")
    if arguments.youtube_owner_manual_live:
        if os.environ.get("VERCEL") or not sys.platform.startswith("linux"):
            parser.error("--youtube-owner-manual-live is restricted to a local Linux operator")
        host = socket.gethostname().strip()
        if not host:
            parser.error("could not determine the local host for YouTube authorization")
    # Normal mode enables paid discovery and Bing verification through their
    # existing account preflight. Owner manual YouTube mode suppresses all
    # SerpApi preflight and configures only the local YouTube runtime boundary.
    # Neither mode starts provider work until a user submits Fetch.
    google_settings = local_live_settings()
    if arguments.youtube_owner_manual_live:
        google_settings = replace(
            google_settings, youtube_live_approved=True, youtube_live_host=host,
        )
        print(
            f"OWNER_MANUAL_LIVE enabled for detected Linux host {host}; "
            "YouTube requests remain user-triggered and one query per Fetch."
        )
        if arguments.youtube_owner_force_fresh_test:
            print("One YouTube cache bypass is armed for the next selected Fetch.")
    settings = Settings.from_environment({
        "TREND_SCOUT_ENVIRONMENT": "local",
        "TREND_SCOUT_RUN_MODE": "live",
        "TREND_SCOUT_READ_ONLY": "false",
        "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
        "TREND_SCOUT_OPERATOR_TOKEN": os.environ.get("TREND_SCOUT_OPERATOR_TOKEN", ""),
    })
    application = create_normal_google_live_app(
        enabled=True,
        workers=1,
        settings=settings,
        composition_factory=partial(
            normal_google_live_composition,
            user_agent=settings.wikimedia_user_agent,
            google_settings=google_settings,
            force_refresh_trends=True,
            allow_initial_google_acceptance=True,
            allow_initial_bing_acceptance=True,
            preserve_expired_data=(arguments.youtube_owner_acceptance
                                   or arguments.youtube_owner_manual_live),
            youtube_max_queries=(1 if (arguments.youtube_owner_acceptance
                                       or arguments.youtube_owner_manual_live) else 2),
            youtube_manual_live=arguments.youtube_owner_manual_live,
            youtube_force_fresh_test_once=arguments.youtube_owner_force_fresh_test,
        ),
    )
    run_normal_google_live(application, port=arguments.port)


if __name__ == "__main__":
    main()
