"""Run normal Wikimedia LIVE Fetch locally with ``python -m scripts.run_wikimedia_live_local``."""

from functools import partial

from app.main import create_normal_wikimedia_live_app, run_normal_wikimedia_live
from app.orchestration.normal_wikimedia_live_composition import normal_wikimedia_live_composition
from app.settings import Settings


def main():
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
    run_normal_wikimedia_live(application)


if __name__ == "__main__":
    main()
