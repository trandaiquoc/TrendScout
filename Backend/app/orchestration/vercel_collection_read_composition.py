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
    analytics_overview_only: bool = False,
    collection_workbench_only: bool = False,
    algorithm_results_only: bool = False,
    research_bootstrap_only: bool = False,
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
            or type(analytics_overview_only) is not bool
            or type(collection_workbench_only) is not bool
            or type(algorithm_results_only) is not bool
            or type(research_bootstrap_only) is not bool
            or sum((latest_dataset_summary_only, dashboard_bootstrap_only,
                    analytics_overview_only,
                    collection_workbench_only, algorithm_results_only,
                    research_bootstrap_only)) > 1):
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
        projection_needs_refresh = False
        if (dashboard_bootstrap_only or analytics_overview_only
                or collection_workbench_only or algorithm_results_only
                or research_bootstrap_only):
            available, bootstrap = snapshot.read_dashboard_bootstrap()
            collection_page = (
                bootstrap.get("collections", {}).get("keyword_page")
                if isinstance(bootstrap, dict)
                and isinstance(bootstrap.get("collections"), dict) else None
            )
            collection_available = (
                available and isinstance(bootstrap, dict)
                and isinstance(bootstrap.get("collections"), dict)
                and isinstance(collection_page, dict)
                and isinstance(collection_page.get("items"), list)
                and type(collection_page.get("total")) is int
            )
            algorithm_results = (
                bootstrap.get("algorithm_results")
                if isinstance(bootstrap, dict) else None
            )
            algorithm_results_available = (
                available and isinstance(algorithm_results, dict)
                and isinstance(algorithm_results.get("summary"), list)
                and isinstance(algorithm_results.get("items"), list)
                and algorithm_results.get("limit") == 100
            )
            research_bootstrap = (
                bootstrap.get("research") if isinstance(bootstrap, dict) else None
            )
            research_bootstrap_available = (
                available and isinstance(research_bootstrap, dict)
                and isinstance(research_bootstrap.get("keywords"), dict)
                and isinstance(research_bootstrap.get("research"), dict)
                and isinstance(research_bootstrap["keywords"].get("items"), list)
                and isinstance(research_bootstrap["research"].get("items"), list)
            )
            analytics_overview_available = (
                available and isinstance(bootstrap, dict)
                and isinstance(bootstrap.get("analytics"), dict)
            )
            projection_needs_refresh = (
                available and (
                    (analytics_overview_only and not analytics_overview_available)
                    or (collection_workbench_only and not collection_available)
                    or (algorithm_results_only and not algorithm_results_available)
                    or (research_bootstrap_only and not research_bootstrap_available)
                )
            )
            if available and (
                dashboard_bootstrap_only
                or (analytics_overview_only and analytics_overview_available)
                or (collection_workbench_only and collection_available)
                or (algorithm_results_only and algorithm_results_available)
                or (research_bootstrap_only and research_bootstrap_available)
            ):
                yield SimpleNamespace(
                    collection_service=None,
                    collection_snapshot=snapshot,
                    latest_dataset_summary_available=False,
                    latest_dataset_summary=None,
                    dashboard_bootstrap_available=dashboard_bootstrap_only,
                    dashboard_bootstrap=bootstrap,
                    analytics_overview_available=(
                        analytics_overview_only and analytics_overview_available
                    ),
                    analytics_overview=(
                        bootstrap.get("analytics") if analytics_overview_available else None
                    ),
                    collection_workbench_available=(
                        collection_workbench_only and collection_available
                    ),
                    collection_workbench=(
                        bootstrap.get("collections") if collection_available else None
                    ),
                    algorithm_results_available=(
                        algorithm_results_only and algorithm_results_available
                    ),
                    algorithm_results=(
                        algorithm_results if algorithm_results_available else None
                    ),
                    research_bootstrap_available=(
                        research_bootstrap_only and research_bootstrap_available
                    ),
                    research_bootstrap=(
                        research_bootstrap
                        if research_bootstrap_available else None
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
        if (dashboard_bootstrap_only or analytics_overview_only
                or collection_workbench_only or algorithm_results_only
                or research_bootstrap_only):
            bootstrap = await asyncio.to_thread(
                build_default_dashboard_bootstrap,
                service,
                available_sources=available_sources,
            )
            await asyncio.to_thread(
                snapshot.backfill_dashboard_bootstrap,
                bootstrap,
                replace_existing=projection_needs_refresh,
            )
        yield SimpleNamespace(
            collection_service=service,
            collection_snapshot=snapshot,
            dashboard_bootstrap_available=(dashboard_bootstrap_only and bootstrap is not None),
            dashboard_bootstrap=bootstrap,
            analytics_overview_available=(
                analytics_overview_only and isinstance(bootstrap, dict)
                and isinstance(bootstrap.get("analytics"), dict)
            ),
            analytics_overview=(
                bootstrap.get("analytics")
                if analytics_overview_only and isinstance(bootstrap, dict) else None
            ),
            collection_workbench_available=(
                collection_workbench_only and isinstance(bootstrap, dict)
                and isinstance(bootstrap.get("collections"), dict)
                and isinstance(bootstrap["collections"].get("keyword_page"), dict)
            ),
            collection_workbench=(
                bootstrap.get("collections")
                if collection_workbench_only and isinstance(bootstrap, dict) else None
            ),
            algorithm_results_available=(
                algorithm_results_only and isinstance(bootstrap, dict)
                and isinstance(bootstrap.get("algorithm_results"), dict)
            ),
            algorithm_results=(
                bootstrap.get("algorithm_results")
                if algorithm_results_only and isinstance(bootstrap, dict) else None
            ),
            research_bootstrap_available=(
                research_bootstrap_only and isinstance(bootstrap, dict)
                and isinstance(bootstrap.get("research"), dict)
            ),
            research_bootstrap=(
                bootstrap.get("research")
                if research_bootstrap_only and isinstance(bootstrap, dict) else None
            ),
            available_sources=available_sources,
        )
