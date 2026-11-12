"""Isolated Wikimedia-only SYNTHETIC Fetch composition for D6.2.3 tests."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import AdmissionPolicy, PoolLimit, PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.main import create_app
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.settings import Settings
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.guarded_collector import CollectionLimits
from app.sources.live_gateway import SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.fake import FakeStorage
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "d6_2_3_wikimedia_response.json"
OBSERVATION_DATE = date(2026, 11, 10)
FETCHED_AT = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
SOURCE_IDS = frozenset({
    WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID,
})
RAW_BODY_MARKER = "D6_2_3_RAW_BODY_MUST_NOT_PERSIST"


def fixture_payload():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def synthetic_settings(lock_path: str) -> Settings:
    """Do not inherit machine .env, cloud settings, or provider credentials."""
    return Settings.from_environment({
        "TREND_SCOUT_ENVIRONMENT": "test",
        "TREND_SCOUT_RUN_MODE": "synthetic",
        "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true",
        "TREND_SCOUT_RUN_COOLDOWN_SECONDS": "0",
        "TREND_SCOUT_LOCK_PATH": lock_path,
    })


class SyntheticWikimediaApprovals:
    """Ephemeral test-only gateway contract value; not an owner approval record."""

    def __init__(self):
        self.value = SourceApproval(
            source_id=WIKIMEDIA_SOURCE_ID,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            expires_at=FETCHED_AT + timedelta(minutes=5),
            policy_version="d6-2-3-synthetic-fixture-v1",
            approval_id="offline-test-only",
            source_enabled=True,
            global_enabled=True,
            usage_approved=True,
            retention_approved=True,
            quota_approved=True,
            cost_approved=True,
            security_approved=True,
            distributed_controls_verified=True,
            egress_controls_verified=True,
            telemetry_controls_verified=True,
            scope="PRODUCTION",
        )

    def current(self, source_id):
        return self.value if source_id == WIKIMEDIA_SOURCE_ID else None


def build_synthetic_app(directory: str):
    """Build the real API/worker graph with one MockTransport and three unavailable sources."""
    directory_path = Path(directory)
    store = LocalSQLiteAtomicStateStore(
        directory_path / "synthetic-fetch.sqlite",
        environment_profile="test",
        data_mode="SYNTHETIC",
    )
    policy = AdmissionPolicy(
        enabled_sources=SOURCE_IDS,
        pools={
            "wikimedia": PoolLimit(10, 100, 10, 100, 1, max_inflight=1),
            "serpapi": PoolLimit(10, 100, 10, 100, 1, max_inflight=1),
            "youtube": PoolLimit(10, 100, 10, 100, 1, max_inflight=1),
        },
        plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
        max_starts_per_window=10,
        window_seconds=3600,
        cooldown_seconds=0,
        lease_seconds=30,
        max_attempts=1,
        max_job_seconds=120,
        receipt_seconds=600,
        max_records=200,
        max_status_polls=100,
        enabled=True,
    )
    admission = PublicAdmission(store, policy, state_key="d6-2-3-synthetic")
    calls = {WIKIMEDIA_SOURCE_ID: 0}
    expected_path = (
        "/api/rest_v1/metrics/pageviews/top-per-country/US/all-access/2026/11/10"
    )

    def mock_wikimedia(request):
        calls[WIKIMEDIA_SOURCE_ID] += 1
        if (calls[WIKIMEDIA_SOURCE_ID] != 1 or request.method != "GET"
                or request.url.host != "wikimedia.org" or request.url.path != expected_path
                or request.url.query):
            raise AssertionError("unexpected or duplicate Wikimedia MockTransport request")
        if "TrendScout" not in request.headers.get("user-agent", ""):
            raise AssertionError("synthetic fixture request lacks the fixed test User-Agent")
        return httpx.Response(200, json=fixture_payload())

    transport = httpx.MockTransport(mock_wikimedia)
    runner = GuardedAttemptRunner(admission, clock=lambda: FETCHED_AT)
    wikimedia = WikimediaTopPageGateway.offline_fixture(
        SyntheticWikimediaApprovals(),
        runner,
        user_agent="TrendScout D6.2.3 test/1.0 (https://example.invalid/contact)",
        transport=transport,
        clock=lambda: FETCHED_AT,
    )
    handlers = D5SourceHandlers(
        query_collectors={
            GOOGLE_SOURCE_ID: None,
            BING_SOURCE_ID: None,
            YOUTUBE_SOURCE_ID: None,
        },
        wikimedia=wikimedia,
        plan=DEFAULT_QUERY_PLAN,
        limits=CollectionLimits(max_requests=1, max_results=50),
        observation_date=lambda: OBSERVATION_DATE,
        unavailable_mode="SYNTHETIC",
        utcnow=lambda: FETCHED_AT,
    ).build()

    def checkpoint_grant(source_id, now):
        return CheckpointGrant(
            source_id,
            "SYNTHETIC",
            "d6-2-3-checkpoint-v1",
            now + timedelta(minutes=5),
            300,
            True,
        )

    worker = DistributedFetchWorker(
        admission,
        handlers=handlers,
        resolve_grant=checkpoint_grant,
        utcnow=lambda: FETCHED_AT,
    )
    facade = DistributedPublicFetch(
        admission,
        worker,
        resolve_grant=checkpoint_grant,
        utcnow=lambda: FETCHED_AT,
    )
    settings = synthetic_settings(str(directory_path / "manual-run.lock"))
    app = create_app(settings=settings, storage=FakeStorage(), distributed_public_fetch=facade)
    app.state.d6_2_3_mock_request_counts = calls
    app.state.d6_2_3_wikimedia_gateway = wikimedia
    app.state.d6_2_3_state_store = store
    return app
