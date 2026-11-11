"""Explicit loopback-only synthetic presentation app; never contacts providers."""

from datetime import date, datetime, timedelta, timezone

from app.adapters.wikimedia import SOURCE
from app.main import create_app
from app.models.observations import CollectionResult, DatasetSnapshot, FetchStatus, PageObservation
from app.settings import Settings
from app.storage.fake import FakeStorage

_END = date(2026, 1, 8)
_FETCHED = datetime(2026, 1, 9, tzinfo=timezone.utc)


class DemoFixtureAdapter:
    """Invented fixed observations; dates and values are not live measurements."""

    __trendscout_offline_only__ = True

    async def collect(self):
        days = []
        for offset in range(8):
            day = _END - timedelta(days=offset)
            rows = []
            for rank, (title, views) in enumerate((
                ("Linen_Pants", 500 if offset < 3 else 100),
                ("Kitchen_Organizer", 250 if offset < 3 else 120),
                ("Robot_Gardening", 150),
            ), start=1):
                rows.append(PageObservation(
                    source=SOURCE, country_scope="US", access="all-access",
                    observation_date=day, project="en.wikipedia", article_original=title,
                    article_display=title.replace("_", " "), canonical_keyword=title,
                    views_ceil=views, provider_rank=rank, fetched_at=_FETCHED,
                    source_url=None,
                    raw_record={"article": title, "project": "en.wikipedia", "rank": rank,
                                "views_ceil": views},
                ))
            snapshot = DatasetSnapshot(
                source=SOURCE, country_scope="US", access="all-access", observation_date=day,
                fetched_at=_FETCHED, fetch_status=FetchStatus.SUCCESS, http_status=None,
                item_count=len(rows), request_url="synthetic://local-fixture", request_count=0,
                rejected_count=0,
            )
            days.append((snapshot, rows))
        return CollectionResult(_END, tuple(days))


def create_demo_app():
    # Explicit mapping prevents inherited cloud credentials or .env configuration
    # from selecting a cloud project, provider, or legacy operator token.
    settings = Settings.from_environment({
        "TREND_SCOUT_ENVIRONMENT": "local",
        "TREND_SCOUT_RUN_MODE": "synthetic",
        "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true",
        "TREND_SCOUT_RUN_COOLDOWN_SECONDS": "0",
    })
    return create_app(settings=settings, storage=FakeStorage(), adapter_factory=DemoFixtureAdapter)
