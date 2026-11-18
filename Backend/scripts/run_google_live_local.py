"""Run the local multi-source LIVE web application."""

import argparse
import os
import socket
import sys
from dataclasses import replace
from functools import partial

from app.google_live_settings import GoogleLiveSettings
from app.main import create_normal_google_live_app, run_normal_google_live
from app.orchestration.normal_google_live_composition import (
    normal_google_live_composition,
    owner_manual_runtime_supported,
)
from app.settings import Settings


def _create_firestore_shared_live_client():
    """Create the pinned local demo client only from keyless SA impersonation ADC."""
    import google.auth
    from google.auth import impersonated_credentials
    from google.cloud import firestore

    from app.orchestration.firestore_shared_live_composition import (
        PRODUCTION_FIRESTORE_PROJECT_ID,
    )
    from app.storage.vercel_identity import VERCEL_IMPERSONATED_SERVICE_ACCOUNT

    credentials, _ = google.auth.default(
        scopes=("https://www.googleapis.com/auth/datastore",)
    )
    if (not isinstance(credentials, impersonated_credentials.Credentials)
            or credentials.service_account_email != VERCEL_IMPERSONATED_SERVICE_ACCOUNT):
        raise ValueError(
            "Firestore Shared LIVE requires ADC impersonating the approved backend service account"
        )
    client = firestore.Client(
        project=PRODUCTION_FIRESTORE_PROJECT_ID,
        database="(default)",
        credentials=credentials,
    )
    if (client.project != PRODUCTION_FIRESTORE_PROJECT_ID
            or getattr(client, "_database", None) != "(default)"):
        raise ValueError("Firestore Shared LIVE client target is invalid")
    return client


def local_live_settings():
    """Return the normal Windows local LIVE source policy."""
    return replace(
        GoogleLiveSettings.from_environment(),
        trends_approved=True,
        serp_approved=True,
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
        "--firestore-shared-live", action="store_true",
        help=("Opt this local loopback demo into the existing trendscout-192e4 "
              "(default) shared Firestore state/cache; omitted means SQLite."),
    )
    parser.add_argument(
        "--youtube-owner-acceptance", action="store_true",
        help=("Preserve expired SQLite blobs and limit YouTube to one search query; "
              "the regular YouTube schedule and approval checks still apply."),
    )
    parser.add_argument(
        "--youtube-owner-manual-live", action="store_true",
        help=("Enable explicit owner-triggered YouTube LIVE development on the "
              "designated local Linux or Windows 2 host; "
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
    firestore_client = None
    if arguments.firestore_shared_live:
        if os.environ.get("VERCEL"):
            parser.error("Firestore Shared LIVE is restricted to the local demo runtime")
        try:
            firestore_client = _create_firestore_shared_live_client()
        except Exception as error:
            parser.error(
                "Firestore Shared LIVE requires keyless ADC impersonation of the approved "
                f"backend service account ({type(error).__name__})"
            )
    if arguments.youtube_owner_manual_live:
        if os.environ.get("VERCEL"):
            parser.error("--youtube-owner-manual-live is restricted to local development")
        host = socket.gethostname().strip()
        if not host:
            parser.error("could not determine the local host for YouTube authorization")
        if not owner_manual_runtime_supported(sys.platform, host):
            parser.error("--youtube-owner-manual-live is restricted to an approved owner host")
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
            f"OWNER_MANUAL_LIVE enabled for detected local owner host {host}; "
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
            shared_firestore_client=firestore_client,
        ),
    )
    run_normal_google_live(application, port=arguments.port)


if __name__ == "__main__":
    main()
