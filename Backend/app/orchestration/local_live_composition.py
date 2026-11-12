"""Compose guarded local live components without opening an HTTP endpoint.

Constructing this context sends no provider requests and grants no permissions.
The caller owns the approval and retention decisions and must separately secure
any future HTTP integration. It is not a production or distributed runtime.
"""

import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app import local_ledger
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.local_smoke_policy import local_smoke_policy
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.sources.guarded_collector import CollectionLimits, GuardedQueryCollector
from app.sources.live_gateway import LivePageGateway
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.sources.wikimedia.live_gateway import WikimediaTopPageGateway
from app.storage.local_smoke_store import LocalSmokeStore
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_volatile_checkpoints import LocalVolatileCheckpoints
from app.workers.d5_source_handlers import D5SourceHandlers
from app.workers.guarded_attempt import GuardedAttemptRunner

_MAX_CHECKPOINT_SECONDS = 120


def _bounded_checkpoint_resolver(resolver):
    """Limit checkpoint references, without granting or extending retention."""
    def resolve(source, now):
        grant = resolver(source, now)
        if (not isinstance(grant, CheckpointGrant) or grant.source_id != source
                or grant.data_mode != "LIVE" or grant.allowed is not True):
            return None
        return replace(grant, max_retention_seconds=min(
            grant.max_retention_seconds, _MAX_CHECKPOINT_SECONDS))
    return resolve


@asynccontextmanager
async def local_live_composition(*, credentials, approvals, user_agent,
                                 resolve_checkpoint_grant, resolve_processing_grants,
                                 utcnow=lambda: datetime.now(timezone.utc)):
    """Build a single-process bounded facade; constructing it never runs Fetch."""
    if (os.environ.get("VERCEL") or not callable(getattr(credentials, "resolve", None))
            or not callable(getattr(approvals, "current", None))
            or not callable(resolve_checkpoint_grant)
            or not callable(resolve_processing_grants) or not callable(utcnow)):
        raise ValueError("explicit local credentials, approvals and policy resolvers required")
    checkpoint_grant = _bounded_checkpoint_resolver(resolve_checkpoint_grant)
    async with AsyncExitStack() as stack:
        local_ledger.DIRECTORY.mkdir(parents=True, mode=0o700, exist_ok=True)
        sqlite = LocalSQLiteAtomicStateStore.for_local_live(
            local_ledger.DIRECTORY / "ledger.sqlite")
        volatile = LocalVolatileCheckpoints(sqlite, clock=utcnow)
        stack.callback(volatile.close)
        admission = PublicAdmission(LocalSmokeStore(volatile),
            replace(local_smoke_policy(), enabled=True), state_key=local_ledger.STATE_KEY)
        runner = GuardedAttemptRunner(admission, clock=utcnow)
        collectors = {}
        for source in (GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID):
            gateway = await LivePageGateway.controlled_network(
                source, credentials, approvals, runner, clock=utcnow)
            stack.push_async_callback(gateway.aclose)
            collectors[source] = GuardedQueryCollector(gateway, utcnow=utcnow)
        wiki = await WikimediaTopPageGateway.controlled_network(
            approvals, runner, user_agent=user_agent, clock=utcnow)
        stack.push_async_callback(wiki.aclose)
        handlers = D5SourceHandlers(query_collectors=collectors, wikimedia=wiki,
            plan=DEFAULT_QUERY_PLAN, limits=CollectionLimits(max_queries=1,
                max_pages_per_query=1, max_requests=1, max_retries_per_page=0,
                deadline_seconds=60, max_results=10),
            observation_date=lambda: (utcnow() - timedelta(days=2)).date(), utcnow=utcnow).build()
        worker = DistributedFetchWorker(admission, handlers=handlers,
            resolve_grant=checkpoint_grant, utcnow=utcnow)
        yield DistributedPublicFetch(admission, worker, resolve_grant=checkpoint_grant,
            utcnow=utcnow, local_live=True, resolve_processing_grants=resolve_processing_grants)
