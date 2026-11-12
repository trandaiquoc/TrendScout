"""Explicit loopback-only synthetic presentation app; never contacts providers."""

import hashlib
import tempfile
from datetime import date, datetime, timedelta, timezone

from app.adapters.wikimedia import SOURCE
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import AdmissionPolicy, PoolLimit, PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.main import create_app
from app.models.observations import CollectionResult, DatasetSnapshot, FetchStatus, PageObservation
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.storage.fake import FakeStorage
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.synthetic_multisource import collect_synthetic_discovery

_END = date(2026, 1, 8)
_FETCHED = datetime(2026, 1, 9, tzinfo=timezone.utc)
_DEMO_SOURCES = frozenset({
    WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
})


def _demo_distributed_fetch(directory):
    store = LocalSQLiteAtomicStateStore(
        f"{directory}/fetch-state.sqlite", environment_profile="local",
        data_mode="SYNTHETIC",
    )
    pools = {
        "wikimedia": PoolLimit(4, 20, 4, 20, 1, max_inflight=2),
        "serpapi": PoolLimit(4, 20, 4, 20, 1, max_inflight=2),
        "youtube": PoolLimit(4, 20, 4, 20, 1, max_inflight=1),
    }
    policy = AdmissionPolicy(
        enabled_sources=_DEMO_SOURCES, pools=pools,
        plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        max_starts_per_window=4, window_seconds=3600, cooldown_seconds=0,
        lease_seconds=30, max_attempts=1, max_job_seconds=60,
        receipt_seconds=600, max_records=100, max_status_polls=30, enabled=True,
    )
    admission = PublicAdmission(store, policy, state_key="demo-public-fetch")
    queries = {source_id: {
        "canonical_query": "TrendScout synthetic presentation fixture",
        "query_fingerprint": hashlib.sha256(
            f"demo:{source_id}".encode("utf-8")
        ).hexdigest(),
    } for source_id in _DEMO_SOURCES}

    def handler_for(source_id):
        def handle(_context):
            fetched = datetime.now(timezone.utc).isoformat()
            common = {"fetched_at": fetched, "source_url": None}
            if source_id == WIKIMEDIA_SOURCE_ID:
                record = {
                    **common, "article_display": "Quiet luxury fashion guide",
                    "article_original": "Quiet_luxury_fashion_guide",
                    "project": "en.wikipedia", "country_scope": "US",
                    "access": "all-access", "observation_date": date.today().isoformat(),
                }
            elif source_id in {GOOGLE_SOURCE_ID, BING_SOURCE_ID}:
                record = {
                    **common, "canonical_url": f"https://example.org/{source_id}",
                    "title": "Quiet luxury fashion guide",
                    "snippet": "Synthetic fashion discovery example",
                    "evidence": [{"query": queries[source_id]}],
                }
            else:
                record = {
                    **common, "provider_video_id": "SyntheticVideo01",
                    "canonical_video_url": "https://example.org/synthetic-video",
                    "title": "Quiet luxury fashion video",
                    "description": "Synthetic fashion discovery example",
                    "query_evidence": [queries[source_id]],
                }
            return {"status": "SUCCESS", "data_mode": "SYNTHETIC",
                    "records": [record], "record_count": 1,
                    "quality_flags": ["SYNTHETIC_FIXTURE"]}

        handle.__trendscout_data_mode__ = "SYNTHETIC"
        return handle

    handlers = {source_id: handler_for(source_id) for source_id in _DEMO_SOURCES}

    def resolve_grant(source_id, now):
        return CheckpointGrant(
            source_id, "SYNTHETIC", "local-demo-synthetic-v1",
            now + timedelta(minutes=5), 120, True,
        )

    worker = DistributedFetchWorker(
        admission, handlers=handlers, resolve_grant=resolve_grant,
    )
    return DistributedPublicFetch(
        admission, worker, resolve_grant=resolve_grant,
    )


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
    demo_directory = tempfile.TemporaryDirectory(prefix="trendscout-local-demo-")
    application = create_app(
        settings=settings, storage=FakeStorage(), adapter_factory=DemoFixtureAdapter,
        synthetic_supplement=collect_synthetic_discovery,
        distributed_public_fetch=_demo_distributed_fetch(demo_directory.name),
    )
    # Keep this local, synthetic-only state alive for the application session.
    # TemporaryDirectory removes it when the demo app is released or exits.
    application.state.demo_directory = demo_directory
    return application
