"""Request-scoped Vercel Wikimedia LIVE worker on the shared Firestore database.

This first online slice deliberately exposes only Wikimedia. Other source
handlers remain unavailable until their provider approvals and budgets are
wired into a later change.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from app.discovery.contracts import ProcessingGrant
from app.google_live_settings import GoogleLiveSettings
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant
from app.normal_wikimedia_live_policy import (
    GOOGLE_POLICY_VERSION,
    GOOGLE_STATE_KEY,
    normal_google_live_policy,
)
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.orchestration.firestore_shared_live_composition import _client_project_id
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.sources.guarded_collector import CollectionLimits
from app.sources.live_gateway import SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
from app.storage.firestore_snapshot_blobs import FirestoreSnapshotBlobs
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

_CACHE_RETENTION_SECONDS = 7 * 24 * 60 * 60


class _VercelWikimediaApproval:
    """Short-lived backend approval scoped to the pinned Vercel Firestore worker."""

    def __init__(self, clock):
        self.clock = clock
        self._current = None

    def current(self, source_id):
        if source_id != WIKIMEDIA_SOURCE_ID:
            return None
        now = self.clock()
        if self._current is not None and self._current.expires_at > now:
            return self._current
        self._current = SourceApproval(
            source_id=source_id,
            plan_fingerprint=DEFAULT_QUERY_PLAN.fingerprint,
            expires_at=now + timedelta(minutes=5),
            policy_version=GOOGLE_POLICY_VERSION,
            approval_id=f"{GOOGLE_POLICY_VERSION}:{source_id}:vercel",
            source_enabled=True,
            global_enabled=True,
            usage_approved=True,
            retention_approved=True,
            quota_approved=True,
            cost_approved=True,
            security_approved=True,
            egress_controls_verified=True,
            telemetry_controls_verified=True,
            scope="VERCEL_SHARED_LIVE_WIKIMEDIA",
            local_controls_verified=False,
        )
        return self._current


def _unavailable_handler(source_id):
    async def unavailable(_context):
        return {
            "status": "UNAVAILABLE",
            "data_mode": "LIVE",
            "records": [],
            "record_count": 0,
            "error_code": "SOURCE_NOT_ENABLED_FOR_VERCEL",
            "coverage": {"provider_request_count": 0},
        }

    unavailable.__trendscout_data_mode__ = "LIVE"
    unavailable.__name__ = f"vercel_unavailable_{source_id}"
    return unavailable


@asynccontextmanager
async def vercel_wikimedia_live_composition(
    client,
    *,
    user_agent: str,
    utcnow=lambda: datetime.now(timezone.utc),
):
    """Yield a Firestore-backed Wikimedia worker; construction sends no provider request."""
    if os.environ.get("VERCEL") != "1" or not isinstance(user_agent, str) or not user_agent.strip():
        raise ValueError("Vercel Wikimedia LIVE requires its deployed runtime")
    if not callable(utcnow):
        raise ValueError("timezone-aware clock required")
    _client_project_id(client)

    google = GoogleLiveSettings.from_environment()
    policy = normal_google_live_policy(
        serpapi_daily_budget=google.serpapi_daily_budget,
        serpapi_monthly_budget=google.serpapi_monthly_budget,
        rss_daily_budget=google.rss_daily_budget,
        rss_monthly_budget=google.rss_monthly_budget,
        fetch_starts_per_hour=google.fetch_starts_per_hour,
        # Share the same quota-policy shape as the Windows 2 Firestore LIVE
        # launcher. Bing remains unavailable below; this reserves the shared
        # pool without enabling its adapter or making provider requests.
        bing_enabled=True,
        bing_daily_budget=google.bing_daily_budget,
        youtube_enabled=True,
    )
    state_store = FirestoreAtomicStateStore(client)
    snapshot_blobs = FirestoreSnapshotBlobs(client)
    admission = PublicAdmission(state_store, policy, state_key=GOOGLE_STATE_KEY)
    runner = GuardedAttemptRunner(admission, clock=utcnow)
    approvals = _VercelWikimediaApproval(utcnow)
    from app.orchestration.normal_wikimedia_live_composition import _PersistentOneSecondPacer

    pacer = _PersistentOneSecondPacer(state_store, clock=utcnow)
    wiki = await WikimediaTopPageGateway.controlled_network(
        approvals,
        runner,
        user_agent=user_agent,
        clock=utcnow,
        before_request=pacer,
        on_rate_limited=pacer.defer,
    )
    try:
        cache = SharedCache(state_store, payload_blobs=snapshot_blobs, max_inline_bytes=500_000)

        def resolve_cache_grant(source_id, now):
            if source_id not in policy.enabled_sources:
                return None
            return CacheGrant(
                source_id,
                GOOGLE_POLICY_VERSION,
                now + timedelta(seconds=_CACHE_RETENTION_SECONDS),
                max_fresh_seconds=(24 * 60 * 60 if source_id == WIKIMEDIA_SOURCE_ID
                                   else 60 * 60),
                max_stale_seconds=_CACHE_RETENTION_SECONDS,
                max_retention_seconds=_CACHE_RETENTION_SECONDS,
                allowed=True,
            )

        def resolve_checkpoint(source_id, now):
            if source_id not in policy.enabled_sources:
                return None
            return CheckpointGrant(
                source_id,
                "LIVE",
                GOOGLE_POLICY_VERSION,
                now + timedelta(seconds=policy.receipt_seconds),
                policy.receipt_seconds,
                True,
            )

        def resolve_processing(now):
            return (ProcessingGrant(
                WIKIMEDIA_SOURCE_ID, True, False,
                now + timedelta(minutes=15), GOOGLE_POLICY_VERSION,
            ),)

        built_handlers = D5SourceHandlers(
            query_collectors={
                "google_web_serp": None,
                "bing_web_serp": None,
                "youtube_video_search": None,
            },
            wikimedia=wiki,
            plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(
                max_queries=1,
                max_pages_per_query=1,
                max_requests=3,
                max_retries_per_page=0,
                deadline_seconds=45,
            ),
            observation_date=lambda: (
                utcnow().astimezone(timezone.utc) - timedelta(days=2)
            ).date(),
            shared_cache=cache,
            resolve_cache_grant=resolve_cache_grant,
            max_wikimedia_date_attempts=3,
            include_wikimedia_transport_metrics=True,
            unavailable_mode="LIVE",
            utcnow=utcnow,
        ).build()
        handlers = {
            source_id: (built_handlers[source_id]
                        if source_id == WIKIMEDIA_SOURCE_ID
                        else _unavailable_handler(source_id))
            for source_id in policy.enabled_sources
        }
        worker = DistributedFetchWorker(
            admission,
            handlers=handlers,
            resolve_grant=resolve_checkpoint,
            utcnow=utcnow,
            snapshot_blobs=snapshot_blobs,
        )
        facade = DistributedPublicFetch(
            admission,
            worker,
            resolve_grant=resolve_checkpoint,
            utcnow=utcnow,
            firestore_shared_live=True,
            resolve_processing_grants=resolve_processing,
        )
        facade.normal_google_live_sources = frozenset(policy.enabled_sources)
        facade.available_sources = frozenset({WIKIMEDIA_SOURCE_ID})
        facade.vercel_wikimedia_live = True
        facade.google_capabilities = {
            "trends_discovery": "NOT_ENABLED_FOR_VERCEL",
            "rss_discovery": "NOT_ENABLED_FOR_VERCEL",
            "serp_verification": "NOT_ENABLED_FOR_VERCEL",
            "bing_serp_verification": "NOT_ENABLED_FOR_VERCEL",
            "account_api": "NOT_CHECKED",
            "live_verified": False,
        }
        yield facade
    finally:
        await wiki.aclose()
