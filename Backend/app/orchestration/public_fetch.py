"""Bounded process-local admission for the offline public Fetch demonstration."""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.orchestration.runs import TERMINAL, RunBusy, RunCooldown
from app.sources.query_plan import get_query_plan

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
FETCH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
FETCH_TTL_SECONDS = 3600
FETCH_WINDOW_SECONDS = 3600
MAX_FETCHES_PER_WINDOW = 3
MAX_STATUS_POLLS = 120
MAX_PUBLIC_CONCURRENCY = 1
PUBLIC_SOURCE_IDS = ("wikimedia_pageviews_top_per_country",)


class PublicFetchBusy(Exception):
    """Another public fetch is still active."""


class PublicFetchRateLimited(Exception):
    """The process-local public admission window is exhausted."""


class PublicFetchNotFound(Exception):
    """The opaque fetch ID is unknown or expired."""


class PublicFetchPollLimit(Exception):
    """The bounded status-poll allowance is exhausted."""


class SingleProcessGuard:
    """Hold one local filesystem lock for the lifetime of an enabled app process."""

    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()
        self._file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        handle = os.fdopen(fd, "r+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None


@dataclass
class FetchRecord:
    fetch_id: str
    run_id: str
    key_digest: str
    fingerprint: str
    created_at: str
    expires_at_monotonic: float
    poll_count: int = 0


def request_fingerprint(settings) -> str:
    """Hash server-selected operation inputs; no request-controlled source fields exist."""
    plan = {
        "operation": "public_fetch_v1",
        "data_mode": "SYNTHETIC",
        "source_ids": list(PUBLIC_SOURCE_IDS),
        "query_plan_id": settings.query_plan_id,
        "query_plan_version": settings.query_plan_version,
        "query_plan_fingerprint": get_query_plan(
            settings.query_plan_id, settings.query_plan_version
        ).fingerprint,
        "request_budget_per_source": 1,
        "max_pages": 1,
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PublicFetchRegistry:
    """Atomic idempotency and global admission within one explicitly local process."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: int = FETCH_TTL_SECONDS,
        window_seconds: int = FETCH_WINDOW_SECONDS,
        max_fetches_per_window: int = MAX_FETCHES_PER_WINDOW,
        max_status_polls: int = MAX_STATUS_POLLS,
    ) -> None:
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._window_seconds = window_seconds
        self.max_fetches_per_window = max_fetches_per_window
        self._max_status_polls = max_status_polls
        self._by_key: dict[str, FetchRecord] = {}
        self._by_id: dict[str, FetchRecord] = {}
        self._accepted_at: deque[float] = deque()
        self._active_fetch_id: str | None = None
        self._lock = asyncio.Lock()

    async def accept(self, key: str, fingerprint: str, *, start, storage):
        if not IDEMPOTENCY_KEY_RE.fullmatch(key):
            raise ValueError("Invalid Idempotency-Key")
        now = self._clock()
        digest = hashlib.sha256(key.encode("ascii")).hexdigest()
        async with self._lock:
            self._purge(now)
            existing = self._by_key.get(digest)
            if existing:
                if existing.fingerprint != fingerprint:
                    raise ValueError("Idempotency key conflicts with a different request")
                return existing, True

            self._expire_window(now)
            if self._active_fetch_id:
                active = self._by_id.get(self._active_fetch_id)
                if active is not None:
                    run = await storage.get_run(active.run_id)
                    if run and run.get("status") in TERMINAL:
                        self._active_fetch_id = None
                if self._active_fetch_id:
                    raise PublicFetchBusy
            if len(self._accepted_at) >= self.max_fetches_per_window:
                raise PublicFetchRateLimited

            run_id = await start()
            record = FetchRecord(
                fetch_id=secrets.token_urlsafe(32),
                run_id=run_id,
                key_digest=digest,
                fingerprint=fingerprint,
                created_at=datetime.now(timezone.utc).isoformat(),
                expires_at_monotonic=now + self._ttl_seconds,
            )
            self._by_key[digest] = record
            self._by_id[record.fetch_id] = record
            self._accepted_at.append(now)
            self._active_fetch_id = record.fetch_id
            return record, False

    async def get(self, fetch_id: str) -> FetchRecord:
        if not FETCH_ID_RE.fullmatch(fetch_id):
            raise PublicFetchNotFound
        now = self._clock()
        async with self._lock:
            self._purge(now)
            record = self._by_id.get(fetch_id)
            if record is None:
                raise PublicFetchNotFound
            if record.poll_count >= self._max_status_polls:
                raise PublicFetchPollLimit
            record.poll_count += 1
            return record

    async def admission_reason(self, *, storage, run_lock) -> str | None:
        """Return an advisory block reason; POST repeats every check atomically."""
        now = self._clock()
        async with self._lock:
            self._purge(now)
            if self._active_fetch_id:
                active = self._by_id.get(self._active_fetch_id)
                if active is not None:
                    run = await storage.get_run(active.run_id)
                    if run and run.get("status") not in TERMINAL:
                        return "FETCH_ALREADY_RUNNING"
                    self._active_fetch_id = None
            self._expire_window(now)
            if len(self._accepted_at) >= self.max_fetches_per_window:
                return "PUBLIC_FETCH_RATE_LIMITED"
            try:
                run_lock.acquire()
            except RunBusy:
                return "FETCH_ALREADY_RUNNING"
            except RunCooldown:
                return "RUN_COOLDOWN_ACTIVE"
            else:
                run_lock.release()
            return None

    def _purge(self, now: float) -> None:
        expired = [
            record for record in self._by_id.values() if record.expires_at_monotonic <= now
        ]
        for record in expired:
            self._by_id.pop(record.fetch_id, None)
            self._by_key.pop(record.key_digest, None)
            if self._active_fetch_id == record.fetch_id:
                self._active_fetch_id = None
        self._expire_window(now)

    def _expire_window(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._accepted_at and self._accepted_at[0] <= cutoff:
            self._accepted_at.popleft()
