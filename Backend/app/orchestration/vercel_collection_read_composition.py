"""Minimal request-scoped Firestore composition for read-only Collection APIs."""

from __future__ import annotations

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
):
    """Restore persisted Collection data without constructing LIVE source workers.

    The caller uses this only for GET/HEAD routes whose handlers read the
    request-scoped SQLite snapshot. Provider adapters, admission, and quota
    state are not created or contacted.
    """
    if (os.environ.get("VERCEL") != "1" or not isinstance(user_agent, str)
            or not user_agent.strip() or not callable(utcnow)
            or defer_collection_snapshot is not False):
        raise ValueError("Vercel Collection reads require the deployed runtime and snapshot")

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
        snapshot.load()
        store = CollectionStore(collection_path, data_mode_scope="LIVE")
        service = CollectionService(store, fetch_model_inference_enabled=False)
        yield SimpleNamespace(
            collection_service=service,
            collection_snapshot=snapshot,
            available_sources=frozenset({
                BING_SOURCE_ID,
                GOOGLE_SOURCE_ID,
                WIKIMEDIA_SOURCE_ID,
                YOUTUBE_SOURCE_ID,
            }),
        )
