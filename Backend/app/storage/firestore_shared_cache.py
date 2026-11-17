"""Explicit Firestore-backed SharedCache composition.

Callers must inject a client with reviewed credentials and permissions. This
factory does not construct a client, choose a project, or connect to Firestore.
"""

from __future__ import annotations

from app.jobs.shared_cache import SharedCache
from app.storage.firestore_atomic_state import FirestoreAtomicStateStore
from app.storage.firestore_snapshot_blobs import FirestoreSnapshotBlobs


def create_firestore_shared_cache(
    client, *, max_attempts: int = 5, max_inline_bytes: int | None = None,
) -> SharedCache:
    """Build a cache whose atomic records and payload chunks use one client."""
    state = FirestoreAtomicStateStore(client, max_attempts=max_attempts)
    blobs = FirestoreSnapshotBlobs(client, max_attempts=max_attempts)
    return SharedCache(state, payload_blobs=blobs, max_inline_bytes=max_inline_bytes)
