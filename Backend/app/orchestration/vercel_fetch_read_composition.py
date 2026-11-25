"""Read-only Vercel context for Fetch status and Run History routes."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.discovery.contracts import ProcessingGrant
from app.google_live_settings import GoogleLiveSettings
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.source_checkpoints import CheckpointGrant
from app.normal_wikimedia_live_policy import (
    GOOGLE_POLICY_VERSION,
    GOOGLE_STATE_KEY,
    normal_google_live_policy,
)
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.orchestration.firestore_shared_live_composition import _client_project_id
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
from app.sources.youtube.policy import YOUTUBE_POLICY_VERSION
from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
from app.storage.firestore_snapshot_blobs import (
    _WARM_COLLECTION_SNAPSHOT_READ_CACHE,
    FirestoreSnapshotBlobs,
)


@asynccontextmanager
async def vercel_fetch_read_composition(
    client,
    *,
    user_agent: str,
    utcnow=lambda: datetime.now(timezone.utc),
    defer_collection_snapshot: bool = True,
    read_latest_dataset_summary: bool = True,
    restore_collection_snapshot: bool = False,
    allow_collection_restore: bool = False,
    run_history_projection_only: bool = False,
):
    """Compose durable Fetch reads without creating provider clients."""
    if (os.environ.get("VERCEL") != "1" or not isinstance(user_agent, str)
            or not user_agent.strip() or not callable(utcnow)
            or defer_collection_snapshot is not True
            or type(read_latest_dataset_summary) is not bool
            or type(restore_collection_snapshot) is not bool
            or type(allow_collection_restore) is not bool
            or type(run_history_projection_only) is not bool
            or restore_collection_snapshot and not allow_collection_restore
            or restore_collection_snapshot and not read_latest_dataset_summary):
        raise ValueError("Vercel Fetch reads require deferred Collections restoration")
    if run_history_projection_only and (
        restore_collection_snapshot or not allow_collection_restore
        or not read_latest_dataset_summary
    ):
        raise ValueError("Run History projection reads require an optional restore fallback")
    _client_project_id(client)

    google = GoogleLiveSettings.from_environment()
    policy = normal_google_live_policy(
        serpapi_daily_budget=google.serpapi_daily_budget,
        serpapi_monthly_budget=google.serpapi_monthly_budget,
        rss_daily_budget=google.rss_daily_budget,
        rss_monthly_budget=google.rss_monthly_budget,
        fetch_starts_per_hour=google.fetch_starts_per_hour,
        bing_enabled=google.paid_bing_approved,
        bing_daily_budget=google.bing_daily_budget,
        youtube_enabled=True,
    )
    state_store = FirestoreAtomicStateStore(client)
    admission = PublicAdmission(state_store, policy, state_key=GOOGLE_STATE_KEY)
    snapshot_blobs = FirestoreSnapshotBlobs(
        client, read_cache=_WARM_COLLECTION_SNAPSHOT_READ_CACHE,
    )

    def resolve_grant(source_id, now):
        if source_id not in policy.enabled_sources:
            return None
        return CheckpointGrant(
            source_id, "LIVE", GOOGLE_POLICY_VERSION,
            now + timedelta(seconds=policy.receipt_seconds),
            policy.receipt_seconds, True,
        )

    def read_only_handler(_context):
        raise RuntimeError("read-only Fetch composition cannot execute source work")

    handlers = {}
    for source_id in policy.enabled_sources:
        handler = read_only_handler
        setattr(handler, "__trendscout_data_mode__", "LIVE")
        handlers[source_id] = handler
    worker = DistributedFetchWorker(
        admission,
        handlers=handlers,
        resolve_grant=resolve_grant,
        utcnow=utcnow,
        snapshot_blobs=snapshot_blobs,
        source_order=tuple(source for source in (
            GOOGLE_SOURCE_ID, BING_SOURCE_ID, WIKIMEDIA_SOURCE_ID, YOUTUBE_SOURCE_ID,
        ) if source in policy.enabled_sources),
    )

    processed_sources = {WIKIMEDIA_SOURCE_ID, GOOGLE_SOURCE_ID}
    if BING_SOURCE_ID in policy.enabled_sources:
        processed_sources.add(BING_SOURCE_ID)

    def resolve_processing(now):
        return tuple(ProcessingGrant(
            source_id, True, False, now + timedelta(minutes=15), GOOGLE_POLICY_VERSION
        ) for source_id in sorted(processed_sources))

    facade = DistributedPublicFetch(
        admission,
        worker,
        resolve_grant=resolve_grant,
        utcnow=utcnow,
        firestore_shared_live=True,
        resolve_processing_grants=resolve_processing,
    )
    facade.available_sources = frozenset(policy.enabled_sources)
    facade.vercel_shared_live = True
    facade.provider_free_composition = True
    facade.run_history_projection_available = False
    facade.run_history_projection = None
    facade.youtube_capabilities = {
        "policy_status": "APPROVED" if google.youtube_policy_approved else "DRAFT",
        "policy_version": YOUTUBE_POLICY_VERSION,
    }

    def prepare_for_shared_fetch():
        # Keep the legacy quota-preserving migration available without loading
        # the source adapter composition on the ordinary admission path.
        from app.orchestration.vercel_wikimedia_live_composition import (
            _vercel_previous_policy_candidates,
            _VercelWikimediaPolicySync,
        )

        return _VercelWikimediaPolicySync(
            state_store,
            target_policy=policy,
            previous_policies=_vercel_previous_policy_candidates(google),
            state_key=GOOGLE_STATE_KEY,
            utcnow=utcnow,
        ).prepare_for_fetch()

    facade.prepare_for_shared_fetch = prepare_for_shared_fetch

    if read_latest_dataset_summary:
        from app.collections.firestore_snapshot import FirestoreCollectionSnapshot

        with TemporaryDirectory(prefix="trendscout-vercel-fetch-read-") as runtime_directory:
            collection_path = Path(runtime_directory) / "collections.sqlite"
            snapshot = FirestoreCollectionSnapshot(client, collection_path, utcnow=utcnow)
            available, summary = snapshot.read_latest_dataset_summary()
            facade.latest_dataset_summary_available = available
            facade.latest_dataset_summary = summary

            if run_history_projection_only:
                dashboard_projection_available, projection_available, projection = (
                    snapshot.read_run_history_projection()
                )
                if projection_available:
                    facade.run_history_projection_available = True
                    facade.run_history_projection = projection

            def load_collection_snapshot():
                if getattr(facade, "collection_service", None) is not None:
                    return facade.collection_service
                from app.collections.entity_context import WikimediaEntityContextResolver
                from app.collections.service import CollectionService
                from app.collections.store import CollectionStore

                if not getattr(facade, "collection_snapshot_loaded", False):
                    snapshot.load()
                    facade.collection_snapshot_loaded = True
                facade.collection_snapshot = snapshot
                store = CollectionStore(collection_path, data_mode_scope="LIVE")
                snapshot.set_latest_dataset_summary_provider(store.latest_dataset)
                def deny_provider_lookup():
                    raise RuntimeError("provider lookup is unavailable in Fetch recovery context")

                resolver = WikimediaEntityContextResolver(
                    store, before_request=deny_provider_lookup,
                )
                facade.collection_service = CollectionService(
                    store,
                    entity_context_resolver=resolver,
                    fetch_model_inference_enabled=False,
                )
                return facade.collection_service

            if allow_collection_restore:
                facade.collection_snapshot_loaded = False
                facade.collection_snapshot = snapshot
                facade.load_collection_snapshot = load_collection_snapshot
            if restore_collection_snapshot:
                load_collection_snapshot()
            elif run_history_projection_only and not facade.run_history_projection_available:
                service = load_collection_snapshot()
                from app.api.dashboard import build_default_dashboard_bootstrap

                bootstrap = await asyncio.to_thread(
                    build_default_dashboard_bootstrap,
                    service,
                    available_sources=facade.available_sources,
                    **({
                        "latest_summary": facade.latest_dataset_summary,
                    } if facade.latest_dataset_summary_available else {}),
                )
                await asyncio.to_thread(
                    snapshot.backfill_dashboard_bootstrap,
                    bootstrap,
                    replace_existing=dashboard_projection_available,
                )
                projection = bootstrap.get("run_history")
                if isinstance(projection, dict):
                    facade.run_history_projection_available = True
                    facade.run_history_projection = projection
            yield facade
    else:
        yield facade
