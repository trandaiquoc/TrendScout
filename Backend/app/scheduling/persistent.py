"""Durable, backend-owned schedule records and a single-process polling worker.

The worker is deliberately opt-in. Its callback is injected by the runtime so
policy and source admission remain owned by the existing run coordinator.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.storage.local_file_security import secure_private_directory, verify_private_file

UTC = timezone.utc
FREQUENCIES = {"DAILY": timedelta(days=1), "WEEKLY": timedelta(days=7)}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(UTC).isoformat()


def _valid_zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or len(name) > 64:
        raise ValueError("invalid timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("unknown timezone") from None


def _next_run(now: datetime, frequency: str, timezone_name: str, hour: int, minute: int) -> datetime:
    if frequency not in FREQUENCIES:
        raise ValueError("unsupported schedule frequency")
    zone = _valid_zone(timezone_name)
    local_now = now.astimezone(zone)
    days = 1 if frequency == "DAILY" else 7
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate += timedelta(days=(days - candidate.weekday()) % days) if frequency == "WEEKLY" else timedelta(0)
    if candidate <= local_now:
        candidate += timedelta(days=days)
    return candidate.astimezone(UTC)


class PersistentScheduleStore:
    """SQLite schedule ledger with transactional occurrence leases."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().absolute()
        secure_private_directory(self.path.parent)
        if self.path.exists() and self.path.is_symlink():
            raise ValueError("schedule database cannot be a symlink")
        with self._connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    frequency TEXT NOT NULL CHECK(frequency IN ('DAILY','WEEKLY')),
                    timezone TEXT NOT NULL, hour INTEGER NOT NULL, minute INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                    next_run_at TEXT NOT NULL, last_run_at TEXT, last_status TEXT,
                    missed_runs INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_occurrences (
                    id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL REFERENCES schedules(id),
                    scheduled_at TEXT NOT NULL, status TEXT NOT NULL,
                    lease_until TEXT, started_at TEXT, finished_at TEXT, error_code TEXT,
                    UNIQUE(schedule_id, scheduled_at)
                );
                CREATE INDEX IF NOT EXISTS schedule_due ON schedules(enabled,next_run_at);
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            if self.path.exists() and self.path.stat().st_mode & 0o077:
                raise ValueError("schedule database permissions could not be restricted") from None
        verify_private_file(self.path)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    def create(self, *, name: str, frequency: str, timezone_name: str,
               hour: int = 0, minute: int = 0, now: datetime | None = None) -> dict:
        now = now or _utc_now()
        if not name.strip() or len(name.strip()) > 80:
            raise ValueError("schedule name must contain 1 to 80 characters")
        if frequency not in FREQUENCIES or type(hour) is not int or not 0 <= hour <= 23 or type(minute) is not int or not 0 <= minute <= 59:
            raise ValueError("invalid frequency or local time")
        _valid_zone(timezone_name)
        schedule_id = uuid.uuid4().hex
        next_run = _next_run(now, frequency, timezone_name, hour, minute)
        with self._connect() as db:
            db.execute("INSERT INTO schedules(id,name,frequency,timezone,hour,minute,enabled,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?,?)",
                       (schedule_id, name.strip(), frequency, timezone_name, hour, minute, _iso(next_run), _iso(now), _iso(now)))
        return self.get(schedule_id)

    def get(self, schedule_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            return dict(row) if row else None

    def list(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM schedules ORDER BY created_at,id")]

    def set_enabled(self, schedule_id: str, enabled: bool, *, now: datetime | None = None) -> dict | None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        now = now or _utc_now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if row is None:
                db.rollback()
                return None
            next_run = row["next_run_at"]
            if enabled and not row["enabled"]:
                next_run = _iso(_next_run(now, row["frequency"], row["timezone"], row["hour"], row["minute"]))
            db.execute("UPDATE schedules SET enabled=?,next_run_at=?,updated_at=? WHERE id=?",
                       (int(enabled), next_run, _iso(now), schedule_id))
            db.commit()
        return self.get(schedule_id)

    def claim_due(self, *, now: datetime | None = None, lease: timedelta = timedelta(minutes=5)) -> dict | None:
        now = now or _utc_now()
        now_text = _iso(now)
        lease_until = _iso(now + lease)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A crashed worker's expired lease becomes retryable with the same occurrence ID.
            expired = db.execute("SELECT id,schedule_id,scheduled_at FROM schedule_occurrences WHERE status='RUNNING' AND lease_until<=? ORDER BY scheduled_at LIMIT 1", (now_text,)).fetchone()
            if expired:
                db.execute("UPDATE schedule_occurrences SET lease_until=? WHERE id=?", (lease_until, expired["id"]))
                schedule = db.execute("SELECT * FROM schedules WHERE id=? AND enabled=1", (expired["schedule_id"],)).fetchone()
                if schedule:
                    db.commit()
                    return {"occurrence_id": expired["id"], "schedule": dict(schedule), "scheduled_at": expired["scheduled_at"], "recovered": True}
                db.execute("UPDATE schedule_occurrences SET status='SKIPPED',finished_at=?,error_code='SCHEDULE_DISABLED' WHERE id=?", (now_text, expired["id"]))
            row = db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at,id LIMIT 1", (now_text,)).fetchone()
            if row is None:
                db.commit()
                return None
            scheduled_at = row["next_run_at"]
            occurrence_id = uuid.uuid5(uuid.NAMESPACE_URL, f"schedule:{row['id']}:{scheduled_at}").hex
            db.execute("INSERT OR IGNORE INTO schedule_occurrences(id,schedule_id,scheduled_at,status,lease_until,started_at) VALUES(?,?,?,'RUNNING',?,?)",
                       (occurrence_id, row["id"], scheduled_at, lease_until, now_text))
            # Skip missed intervals instead of a burst of catch-up fetches.
            next_run = _iso(_next_run(now, row["frequency"], row["timezone"], row["hour"], row["minute"]))
            missed = max(0, int((now - datetime.fromisoformat(scheduled_at)).total_seconds() // FREQUENCIES[row["frequency"]].total_seconds()))
            db.execute("UPDATE schedules SET next_run_at=?,updated_at=?,missed_runs=missed_runs+? WHERE id=?",
                       (next_run, now_text, missed, row["id"]))
            db.commit()
            return {"occurrence_id": occurrence_id, "schedule": dict(row), "scheduled_at": scheduled_at, "recovered": False}

    def finish(self, occurrence_id: str, *, status: str, now: datetime | None = None, error_code: str | None = None) -> None:
        if status not in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED"}:
            raise ValueError("invalid schedule run status")
        now_text = _iso(now or _utc_now())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            occurrence = db.execute("SELECT schedule_id,status FROM schedule_occurrences WHERE id=?", (occurrence_id,)).fetchone()
            if occurrence is None:
                db.rollback()
                raise ValueError("unknown schedule occurrence")
            if occurrence["status"] in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED"}:
                db.commit()
                return
            db.execute("UPDATE schedule_occurrences SET status=?,lease_until=NULL,finished_at=?,error_code=? WHERE id=?",
                       (status, now_text, error_code, occurrence_id))
            db.execute("UPDATE schedules SET last_run_at=?,last_status=?,updated_at=? WHERE id=?",
                       (now_text, status, now_text, occurrence["schedule_id"]))
            db.commit()


class ScheduleWorker:
    """Cancellable worker; injected callback is the only job execution path."""

    def __init__(self, store: PersistentScheduleStore, run_schedule, *, poll_seconds: float = 5.0):
        if poll_seconds < 0.1 or poll_seconds > 60 or not callable(run_schedule):
            raise ValueError("invalid scheduler worker configuration")
        self.store, self.run_schedule, self.poll_seconds = store, run_schedule, poll_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self):
        if self._task is not None:
            raise RuntimeError("scheduler worker already started")
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="trendscout-scheduler")

    async def _run(self):
        while not self._stop.is_set():
            item = self.store.claim_due()
            if item is not None:
                try:
                    result = await self.run_schedule(item)
                    status = result if result in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED"} else "FAILED"
                    invalid_result = status == "FAILED" and result != "FAILED"
                    self.store.finish(item["occurrence_id"], status=status,
                                      error_code="INVALID_RUN_RESULT" if invalid_result else None)
                except asyncio.CancelledError:
                    self.store.finish(item["occurrence_id"], status="ABORTED", error_code="SHUTDOWN")
                    raise
                except Exception as exc:
                    self.store.finish(item["occurrence_id"], status="FAILED", error_code=type(exc).__name__[:80])
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=30)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None
