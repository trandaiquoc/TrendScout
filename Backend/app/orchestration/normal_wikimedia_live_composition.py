"""Normal, local-only Wikimedia LIVE Fetch composition, separate from D6 smoke."""

import asyncio
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.discovery.contracts import ProcessingGrant
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.jobs.source_checkpoints import CheckpointGrant
from app.normal_wikimedia_live_policy import POLICY_VERSION, STATE_KEY, normal_wikimedia_live_policy
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.sources.guarded_collector import CollectionLimits
from app.sources.live_gateway import SourceApproval
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.local_file_security import secure_private_directory
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

_MAX_CACHE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_PACING_STATE_KEY = f"{STATE_KEY}-pacing"


class _PersistentOneSecondPacer:
    """Reserve one-second GET slots atomically across local app restarts."""

    def __init__(self, store, *, clock):
        self.store, self.clock = store, clock

    async def __call__(self):
        now = self.clock().astimezone(timezone.utc)

        def reserve(state):
            previous_value = state.get("next_allowed_at")
            previous = datetime.fromisoformat(previous_value) if previous_value else now
            if previous.tzinfo is None or previous.utcoffset() is None or now < previous - timedelta(seconds=1):
                raise ValueError("Wikimedia pacing clock regressed")
            slot = max(now, previous)
            return {"next_allowed_at": (slot + timedelta(seconds=1)).isoformat()}, slot

        slot = await asyncio.to_thread(self.store.transact, _PACING_STATE_KEY, reserve)
        delay = (slot - self.clock().astimezone(timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

    async def defer(self, seconds):
        if type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid Wikimedia Retry-After interval")
        now = self.clock().astimezone(timezone.utc)
        seconds = min(float(seconds), 31_536_000)

        def reserve(state):
            previous_value = state.get("next_allowed_at")
            previous = datetime.fromisoformat(previous_value) if previous_value else now
            until = now + timedelta(seconds=seconds)
            return {"next_allowed_at": max(previous, until).isoformat()}, None

        await asyncio.to_thread(self.store.transact, _PACING_STATE_KEY, reserve)

    def retry_after_seconds(self):
        now = self.clock().astimezone(timezone.utc)

        def inspect(state):
            value = state.get("next_allowed_at")
            if not value:
                return state, 0
            try:
                until = datetime.fromisoformat(value)
                if until.tzinfo is None or until.utcoffset() is None:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("Wikimedia pacing state is invalid") from None
            remaining = max(0, math.ceil((until - now).total_seconds()))
            return (state if remaining else {}, remaining)

        return self.store.transact(_PACING_STATE_KEY, inspect)


class _NormalWikimediaApproval:
    """Backend-owned runtime controls for the owner-authorized local mode."""

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
            policy_version=POLICY_VERSION,
            approval_id=POLICY_VERSION,
            source_enabled=True,
            global_enabled=True,
            usage_approved=True,
            retention_approved=True,
            quota_approved=True,
            cost_approved=True,
            security_approved=True,
            egress_controls_verified=True,
            telemetry_controls_verified=True,
            scope="LOCAL_NORMAL",
            local_controls_verified=True,
        )
        return self._current


@asynccontextmanager
async def normal_wikimedia_live_composition(*, user_agent,
                                            utcnow=lambda: datetime.now(timezone.utc),
                                            state_directory=None):
    """Compose one loopback-only live source; construction itself sends no GET."""
    if (os.environ.get("VERCEL") or not isinstance(user_agent, str) or not user_agent.strip()
            or not callable(utcnow)):
        raise ValueError("normal Wikimedia LIVE requires explicit local configuration")
    directory = Path(state_directory) if state_directory is not None else (
        Path.home() / ".local" / "state" / "trendscout" / STATE_KEY
    )
    secure_private_directory(directory)
    store = LocalSQLiteAtomicStateStore.for_local_live(directory / "state.sqlite")
    policy = normal_wikimedia_live_policy()
    admission = PublicAdmission(store, policy, state_key=STATE_KEY)
    runner = GuardedAttemptRunner(admission, clock=utcnow)
    approvals = _NormalWikimediaApproval(utcnow)
    pace_request = _PersistentOneSecondPacer(store, clock=utcnow)
    wiki = await WikimediaTopPageGateway.controlled_network(
        approvals, runner, user_agent=user_agent, clock=utcnow,
        before_request=pace_request,
        on_rate_limited=pace_request.defer,
    )
    try:
        cache = SharedCache(store)

        def resolve_cache_grant(source_id, now):
            if source_id != WIKIMEDIA_SOURCE_ID:
                return None
            return CacheGrant(source_id, POLICY_VERSION,
                now + timedelta(seconds=_MAX_CACHE_RETENTION_SECONDS),
                max_fresh_seconds=24 * 60 * 60,
                max_stale_seconds=_MAX_CACHE_RETENTION_SECONDS,
                max_retention_seconds=_MAX_CACHE_RETENTION_SECONDS,
                allowed=True)

        def resolve_checkpoint(source_id, now):
            if source_id != WIKIMEDIA_SOURCE_ID:
                return None
            return CheckpointGrant(source_id, "LIVE", POLICY_VERSION,
                                   now + timedelta(minutes=15), 600, True)

        def resolve_processing(now):
            return (ProcessingGrant(WIKIMEDIA_SOURCE_ID, True, False,
                                    now + timedelta(minutes=15), POLICY_VERSION),)

        handlers = D5SourceHandlers(
            query_collectors={
                "google_web_serp": None,
                "bing_web_serp": None,
                "youtube_video_search": None,
            },
            wikimedia=wiki,
            plan=DEFAULT_QUERY_PLAN,
            limits=CollectionLimits(max_queries=1, max_pages_per_query=1,
                max_requests=3, max_retries_per_page=0, deadline_seconds=60, max_results=10),
            observation_date=lambda: (utcnow().astimezone(timezone.utc) - timedelta(days=2)).date(),
            shared_cache=cache,
            resolve_cache_grant=resolve_cache_grant,
            max_wikimedia_date_attempts=3,
            include_wikimedia_transport_metrics=True,
            unavailable_mode="LIVE",
            utcnow=utcnow,
        ).build()[WIKIMEDIA_SOURCE_ID]
        worker = DistributedFetchWorker(
            admission, handlers={WIKIMEDIA_SOURCE_ID: handlers},
            resolve_grant=resolve_checkpoint, utcnow=utcnow,
        )
        yield DistributedPublicFetch(
            admission, worker, resolve_grant=resolve_checkpoint, utcnow=utcnow,
            normal_wikimedia_live=True, resolve_processing_grants=resolve_processing,
            provider_retry_after_seconds=pace_request.retry_after_seconds,
        )
    finally:
        await wiki.aclose()
