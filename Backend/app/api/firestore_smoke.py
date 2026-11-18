"""Short-lived, operator-only Production Firestore smoke route.

This route is mounted only when a Production-only Vercel flag is explicitly
enabled. It writes one synthetic cache document under a unique smoke prefix,
reads it through a fresh request-scoped WIF client, and deletes only that exact
document after validating its ownership marker.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.jobs.shared_cache import CacheGrant, CacheScope, SharedCache
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.storage.firestore_atomic_state import FirestoreAtomicStateStore

router = APIRouter()
_RUN_ID = re.compile(r"^[a-f0-9]{32}$")
_SMOKE_PREFIX = "smoke-"
_MAX_ENABLE_WINDOW = timedelta(minutes=30)
_CACHE_TTL_SECONDS = 1800
_PLAN_FINGERPRINT = hashlib.sha256(b"trendscout-firestore-smoke-v1").hexdigest()


class SmokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)


class _SmokeNamespacedStore:
    """Keep the unique smoke key separate from normal shared-state keys."""

    def __init__(self, client):
        self._store = FirestoreAtomicStateStore(client, max_attempts=1)

    def transact(self, key, operation):
        return self._store.transact(f"{_SMOKE_PREFIX}{key}", operation)


def _enabled(request: Request) -> bool:
    if (
        os.environ.get("VERCEL") != "1"
        or os.environ.get("VERCEL_ENV") != "production"
        or os.environ.get("TREND_SCOUT_FIRESTORE_SMOKE_ENABLED") != "1"
        or not getattr(request.app.state, "read_only", False)
    ):
        return False
    expires = os.environ.get("TREND_SCOUT_FIRESTORE_SMOKE_EXPIRES_AT", "")
    try:
        expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        return False
    remaining = expires_at.astimezone(timezone.utc) - datetime.now(timezone.utc)
    return timedelta(0) < remaining <= _MAX_ENABLE_WINDOW


def _authorize(request: Request) -> None:
    if not _enabled(request):
        raise HTTPException(404, "Not found")
    expected = getattr(request.app.state, "operator_token", None)
    supplied = request.headers.get("authorization", "")
    if not isinstance(expected, str) or len(expected) < 32:
        raise HTTPException(404, "Not found")
    if not hmac.compare_digest(
        supplied.encode("utf-8"), f"Bearer {expected}".encode("utf-8")
    ):
        raise HTTPException(401, "Operator authentication required")


def _scope(run_id: str) -> CacheScope:
    return CacheScope(
        source_id=WIKIMEDIA_SOURCE_ID,
        provider_id="wikimedia_aqs",
        query=f"synthetic-firestore-smoke-{run_id}",
        plan_fingerprint=_PLAN_FINGERPRINT,
        market="synthetic-smoke",
        language="en",
        window="one-shot",
        parser_version="smoke-v1",
        policy_version="smoke-v1",
    )


def _grant(scope: CacheScope, now: datetime) -> CacheGrant:
    return CacheGrant(
        source_id=scope.source_id,
        policy_version=scope.policy_version,
        expires_at=now + _MAX_ENABLE_WINDOW,
        max_fresh_seconds=int(_MAX_ENABLE_WINDOW.total_seconds()),
        max_stale_seconds=int(_MAX_ENABLE_WINDOW.total_seconds()),
        max_retention_seconds=int(_MAX_ENABLE_WINDOW.total_seconds()),
        allowed=True,
    )


def _owned_record(snapshot, scope: CacheScope, run_id: str) -> bool:
    if not snapshot.exists:
        return False
    record = snapshot.to_dict() or {}
    return (
        record.get("scope_hash") == scope.key
        and record.get("origin") == "SYNTHETIC"
        and record.get("payload") == {"smoke_run_id": run_id, "sample": "synthetic"}
    )


@router.post("/firestore-smoke/{phase}", include_in_schema=False)
def firestore_smoke(phase: Literal["write", "verify", "cleanup"], body: SmokeRequest,
                    request: Request):
    """Run one bounded phase; no source adapter or provider client is created."""
    _authorize(request)
    try:
        storage = request.app.state.read_storage_factory(
            request.headers.get("x-vercel-oidc-token")
        )
        client = storage._client
        if getattr(client, "project", None) != "trendscout-192e4":
            raise HTTPException(503, "Smoke target is not authorized")
        if getattr(client, "_database", None) != "(default)":
            raise HTTPException(503, "Smoke database is not authorized")

        scope = _scope(body.run_id)
        now = datetime.now(timezone.utc)
        reference = client.collection("_ts_d5_coordination").document(
            f"{_SMOKE_PREFIX}{scope.key}"
        )
        cache = SharedCache(_SmokeNamespacedStore(client))
        grant = _grant(scope, now)
        payload = {"smoke_run_id": body.run_id, "sample": "synthetic"}

        if phase == "write":
            if reference.get().exists:
                raise HTTPException(409, "Smoke namespace is already in use")
            cache.put(scope, grant=grant, origin="SYNTHETIC", payload=payload,
                      fetched_at=now, now=now)
            reference.update({"smoke_run_id": body.run_id, "smoke_phase": "written"})
            return {
                "phase": "write",
                "target": "trendscout-192e4/(default)",
                "synthetic": True,
                "documents_created": 1,
                "firestore_reads": 2,
                "firestore_writes": 2,
                "provider_calls": 0,
            }

        if phase == "verify":
            snapshot = reference.get()
            if not _owned_record(snapshot, scope, body.run_id):
                raise HTTPException(409, "Smoke record is missing or not owned by this run")
            record = snapshot.to_dict() or {}
            if record.get("smoke_phase") == "verified":
                return {
                    "phase": "verify",
                    "cache_status": "CACHED",
                    "replayed": True,
                    "provider_calls": 0,
                    "firestore_reads": 1,
                    "firestore_writes": 0,
                }
            if record.get("smoke_phase") != "written":
                raise HTTPException(409, "Smoke phase is invalid")
            result = cache.get(scope, grant=grant, now=now)
            if (result.get("status") != "CACHED" or result.get("origin") != "SYNTHETIC"
                    or result.get("payload") != payload):
                raise HTTPException(503, "Synthetic cache verification failed")
            reference.update({"smoke_phase": "verified"})
            return {
                "phase": "verify",
                "cache_status": "CACHED",
                "replayed": False,
                "provider_calls": 0,
                "firestore_reads": 2,
                "firestore_writes": 2,
            }

        snapshot = reference.get()
        if not snapshot.exists:
            return {
                "phase": "cleanup",
                "cleaned": True,
                "documents_deleted": 0,
                "verified_absent": True,
                "firestore_reads": 1,
                "firestore_writes": 0,
            }
        if not _owned_record(snapshot, scope, body.run_id):
            raise HTTPException(409, "Smoke record is not owned by this run; left untouched")
        reference.delete()
        remaining = reference.get()
        if remaining.exists:
            raise HTTPException(503, "Smoke cleanup could not be verified")
        return {
            "phase": "cleanup",
            "cleaned": True,
            "documents_deleted": 1,
            "verified_absent": True,
            "firestore_reads": 2,
            "firestore_writes": 1,
        }
    except HTTPException:
        raise
    except Exception:
        # Keep auth tokens, request headers, and provider/library error bodies out
        # of the response and application logs. Outcome-unknown writes are cleaned
        # through the explicit authenticated cleanup phase.
        raise HTTPException(
            503,
            "Smoke phase failed; run the authenticated cleanup phase to verify state",
        ) from None
