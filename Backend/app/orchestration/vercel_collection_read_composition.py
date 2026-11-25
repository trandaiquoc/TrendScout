"""Minimal request-scoped Firestore composition for read-only Collection APIs."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace


@asynccontextmanager
async def vercel_collection_read_composition(
    client,
    *,
    user_agent: str,
    utcnow=lambda: datetime.now(timezone.utc),
    defer_collection_snapshot: bool = False,
    latest_dataset_summary_only: bool = False,
    dashboard_bootstrap_only: bool = False,
    collection_workbench_only: bool = False,
):
    """Restore persisted Collection data without constructing LIVE source workers.

    The caller uses this only for GET/HEAD routes whose handlers read the
    request-scoped SQLite snapshot. Provider adapters, admission, and quota
    state are not created or contacted.
    """
    if (os.environ.get("VERCEL") != "1" or not isinstance(user_agent, str)
            or not user_agent.strip() or not callable(utcnow)
            or defer_collection_snapshot is not False
            or type(latest_dataset_summary_only) is not bool
            or type(dashboard_bootstrap_only) is not bool
            or type(collection_workbench_only) is not bool
            or sum((latest_dataset_summary_only, dashboard_bootstrap_only,
                    collection_workbench_only)) > 1):
        raise ValueError("Vercel Collection reads require the deployed runtime and snapshot")

    from app.api.dashboard import build_default_dashboard_bootstrap
    from app.collections.firestore_snapshot import FirestoreCollectionSnapshot
    from app.collections.service import CollectionService
    from app.collections.store import CollectionStore
    from app.orchestration.firestore_shared_live_composition import _client_project_id
    from app.sources.catalog import (
        BING_SOURCE_ID,
        GOOGLE_SOURCE_ID,
        WIKIMEDIA_SOURCE_ID,
        YOUTUBE_SOURCE_ID,
    )

    _client_project_id(client)
    with TemporaryDirectory(prefix="trendscout-vercel-read-") as runtime_directory:
        collection_path = Path(runtime_directory) / "collections.sqlite"
        snapshot = FirestoreCollectionSnapshot(client, collection_path, utcnow=utcnow)
        available_sources = frozenset({
            BING_SOURCE_ID,
            GOOGLE_SOURCE_ID,
            WIKIMEDIA_SOURCE_ID,
            YOUTUBE_SOURCE_ID,
        })
        if dashboard_bootstrap_only or collection_workbench_only:
            available, bootstrap = snapshot.read_dashboard_bootstrap()
            collection_available = (
                available and isinstance(bootstrap, dict)
                and isinstance(bootstrap.get("collections"), dict)
            )
            if available and (dashboard_bootstrap_only or collection_available):
                yield SimpleNamespace(
                    collection_service=None,
                    collection_snapshot=snapshot,
                    latest_dataset_summary_available=False,
                    latest_dataset_summary=None,
                    dashboard_bootstrap_available=dashboard_bootstrap_only,
                    dashboard_bootstrap=bootstrap,
                    collection_workbench_available=(
                        collection_workbench_only and collection_available
                    ),
                    collection_workbench=(
                        bootstrap.get("collections") if collection_available else None
                    ),
                    available_sources=available_sources,
                )
                return
        if latest_dataset_summary_only:
            available, summary = snapshot.read_latest_dataset_summary()
            if available:
                yield SimpleNamespace(
                    collection_service=None,
                    collection_snapshot=snapshot,
                    latest_dataset_summary_available=True,
                    latest_dataset_summary=summary,
                    available_sources=available_sources,
                )
                return
        snapshot.load()
        store = CollectionStore(collection_path, data_mode_scope="LIVE")
        service = CollectionService(store, fetch_model_inference_enabled=False)
        bootstrap = None
        if dashboard_bootstrap_only or collection_workbench_only:
            bootstrap = await asyncio.to_thread(
                build_default_dashboard_bootstrap,
                service,
                available_sources=available_sources,
            )
            await asyncio.to_thread(snapshot.backfill_dashboard_bootstrap, bootstrap)
        yield SimpleNamespace(
            collection_service=service,
            collection_snapshot=snapshot,
            dashboard_bootstrap_available=(dashboard_bootstrap_only and bootstrap is not None),
            dashboard_bootstrap=bootstrap,
            collection_workbench_available=(
                collection_workbench_only and isinstance(bootstrap, dict)
                and isinstance(bootstrap.get("collections"), dict)
            ),
            collection_workbench=(
                bootstrap.get("collections")
                if collection_workbench_only and isinstance(bootstrap, dict) else None
            ),
            available_sources=available_sources,
        )
