"""Run the local multi-source LIVE web application."""

import argparse
import os
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
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    # The local web demo enables Google discovery and one-candidate Bing
    # verification. Both require user Fetch, a fresh Account API preflight,
    # persistent source schedules and the shared provider/source budgets.
    # Shell and PowerShell launchers only start this server-owned policy.
    google_settings = local_live_settings()
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
            preserve_expired_data=arguments.youtube_owner_acceptance,
            youtube_max_queries=1 if arguments.youtube_owner_acceptance else 2,
        ),
    )
    run_normal_google_live(application, port=arguments.port)


if __name__ == "__main__":
    main()
