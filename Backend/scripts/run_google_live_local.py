"""Run the separately opted-in Wikimedia + Google LIVE profile locally."""

import argparse
import os
from functools import partial

from app.google_live_settings import GoogleLiveSettings
from app.main import create_normal_google_live_app, run_normal_google_live
from app.orchestration.normal_google_live_composition import normal_google_live_composition
from app.settings import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the separately approved local Google LIVE profile."
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("TREND_SCOUT_DEMO_PORT", "8000")),
    )
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    google_settings = GoogleLiveSettings.from_environment()
    if not google_settings.local_runtime_approved:
        parser.error(
            "set TREND_SCOUT_GOOGLE_LOCAL_LIVE_APPROVED=true before using this separate profile"
        )
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
        ),
    )
    run_normal_google_live(application, port=arguments.port)


if __name__ == "__main__":
    main()
