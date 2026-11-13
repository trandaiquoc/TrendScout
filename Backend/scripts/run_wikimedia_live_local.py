"""Run normal Wikimedia LIVE Fetch locally with ``python -m scripts.run_wikimedia_live_local``."""

import argparse
import os
from functools import partial

from app.main import create_normal_wikimedia_live_app, run_normal_wikimedia_live
from app.orchestration.normal_wikimedia_live_composition import normal_wikimedia_live_composition
from app.settings import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run normal Wikimedia LIVE Fetch locally.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TREND_SCOUT_DEMO_PORT", "8000")))
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    settings = Settings.from_environment({
        "TREND_SCOUT_ENVIRONMENT": "local",
        "TREND_SCOUT_RUN_MODE": "live",
        "TREND_SCOUT_READ_ONLY": "false",
        "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
    })
    application = create_normal_wikimedia_live_app(
        enabled=True,
        workers=1,
        settings=settings,
        composition_factory=partial(
            normal_wikimedia_live_composition,
            user_agent=settings.wikimedia_user_agent,
        ),
    )
    run_normal_wikimedia_live(application, port=arguments.port)


if __name__ == "__main__":
    main()
