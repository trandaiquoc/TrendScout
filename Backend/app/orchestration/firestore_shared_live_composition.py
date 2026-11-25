"""Explicit, injected Firestore composition for the shared LIVE fetch worker.

This module never creates a Firestore client or credentials. Callers must pass
the reviewed backend client for ``trendscout-192e4``. Existing compositions use
SQLite by default; local Google LIVE can explicitly inject this shared backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from app.jobs.distributed_worker import (
    DistributedFetchWorker,
    GrantResolver,
    SourceHandler,
)
from app.jobs.public_admission import AdmissionPolicy, PublicAdmission
from app.jobs.shared_cache import SharedCache
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
from app.storage.firestore_emulator import (
    SYNTHETIC_PROJECT_ID,
    GuardedFirestoreEmulatorClient,
)
from app.storage.firestore_snapshot_blobs import FirestoreSnapshotBlobs

PRODUCTION_FIRESTORE_PROJECT_ID = "trendscout-192e4"
_SUPPORTED_PROJECT_IDS = frozenset({PRODUCTION_FIRESTORE_PROJECT_ID, SYNTHETIC_PROJECT_ID})


@dataclass(frozen=True)
class FirestoreSharedLiveComposition:
    """All shared LIVE components built around one explicitly injected client."""

    project_id: str
    state_store: FirestoreAtomicStateStore
    snapshot_blobs: FirestoreSnapshotBlobs
    shared_cache: SharedCache
    admission: PublicAdmission
    worker: DistributedFetchWorker
    public_fetch: DistributedPublicFetch


def _client_project_id(client) -> str:
    if isinstance(client, GuardedFirestoreEmulatorClient):
        project_id = client.project_id
        if project_id != SYNTHETIC_PROJECT_ID:
            raise ValueError("guarded emulator client must use the dedicated synthetic project")
    else:
        project_id = getattr(client, "project", None)
        if project_id != PRODUCTION_FIRESTORE_PROJECT_ID:
            raise ValueError("production Firestore client must target trendscout-192e4")

    if project_id not in _SUPPORTED_PROJECT_IDS:
        raise ValueError("unsupported Firestore project")
    # google-cloud-firestore is pinned in pyproject.toml and exposes the
    # selected database on this client field; fail closed for named databases.
    if getattr(client, "_database", None) != "(default)":
        raise ValueError("Firestore client must target the existing (default) database")
    return project_id


def create_firestore_shared_live_composition(
    client,
    *,
    policy: AdmissionPolicy,
    state_key: str,
    handlers_factory: Callable[[SharedCache], Mapping[str, SourceHandler]],
    resolve_grant: GrantResolver,
    resolve_processing_grants: Callable[[datetime], object],
    utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    provider_retry_after_seconds: Callable[[], int] | None = None,
    max_attempts: int = 5,
    cache_max_inline_bytes: int | None = None,
) -> FirestoreSharedLiveComposition:
    """Build a shared LIVE fetch stack without creating credentials or clients.

    ``handlers_factory`` receives the Firestore-backed cache so source handlers
    cannot accidentally use a separate cache instance. Handler construction
    must not issue provider requests; actual attempts stay under each handler's
    existing approval and admission controls.
    """
    project_id = _client_project_id(client)
    if (not isinstance(policy, AdmissionPolicy)
            or not isinstance(state_key, str)
            or not callable(handlers_factory)
            or not callable(resolve_grant)
            or not callable(resolve_processing_grants)
            or not callable(utcnow)
            or (provider_retry_after_seconds is not None
                and not callable(provider_retry_after_seconds))):
        raise ValueError("explicit Firestore policy, handlers, and grant resolvers are required")

    state_store = FirestoreAtomicStateStore(client, max_attempts=max_attempts)
    snapshot_blobs = FirestoreSnapshotBlobs(client, max_attempts=max_attempts)
    shared_cache = SharedCache(
        state_store,
        payload_blobs=snapshot_blobs,
        max_inline_bytes=cache_max_inline_bytes,
        monotonic_read_clock=True,
    )
    admission = PublicAdmission(state_store, policy, state_key=state_key)
    handlers = handlers_factory(shared_cache)
    worker = DistributedFetchWorker(
        admission,
        handlers=handlers,
        resolve_grant=resolve_grant,
        utcnow=utcnow,
        snapshot_blobs=snapshot_blobs,
    )
    public_fetch = DistributedPublicFetch(
        admission,
        worker,
        resolve_grant=resolve_grant,
        utcnow=utcnow,
        firestore_shared_live=True,
        resolve_processing_grants=resolve_processing_grants,
        provider_retry_after_seconds=provider_retry_after_seconds,
    )
    return FirestoreSharedLiveComposition(
        project_id=project_id,
        state_store=state_store,
        snapshot_blobs=snapshot_blobs,
        shared_cache=shared_cache,
        admission=admission,
        worker=worker,
        public_fetch=public_fetch,
    )
