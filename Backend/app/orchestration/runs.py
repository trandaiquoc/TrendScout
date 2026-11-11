"""Local-host manual run coordinator and durable Firestore persistence."""

import asyncio
import fcntl
import json
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.adapters.base import CollectionSource
from app.models.observations import FetchStatus
from app.ports.storage import AnalysisStorage

TERMINAL = frozenset({"COMPLETED", "PARTIAL", "FAILED", "ABORTED"})
SHARDS = 16
CHUNK_SIZE = 40
MAX_DOC_BYTES = 700_000
MAX_RUN_WRITES = 1000


def now():
    return datetime.now(timezone.utc)


def safe_doc(data):
    if len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")) > MAX_DOC_BYTES:
        raise ValueError("Document exceeds conservative storage size limit")
    return data


class RunBusy(Exception):
    pass


class RunCooldown(Exception):
    pass


class HostRunLock:
    """flock serializes workers on one Ubuntu host, including separate processes."""
    def __init__(self, path: str, cooldown_seconds: int = 60):
        self.path = Path(path).expanduser()
        self.cooldown_seconds = cooldown_seconds
        self.file = None

    def acquire(self, *, ignore_cooldown=False):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        handle = os.fdopen(fd, "r+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RunBusy("A run is already active")
        self.file = handle
        try:
            content = handle.read()
            state = json.loads(content) if content.strip() else {}
            if not isinstance(state, dict):
                raise ValueError("Invalid lock state")
            last = state.get("last_finished_at")
            if not ignore_cooldown and last and now() - datetime.fromisoformat(last) < timedelta(seconds=self.cooldown_seconds):
                raise RunCooldown("Wait before starting another run")
            return state
        except Exception:
            self.release()
            raise

    def write(self, data):
        self.file.seek(0)
        self.file.truncate()
        json.dump(data, self.file)
        self.file.flush()
        os.fsync(self.file.fileno())

    def release(self):
        if self.file is not None:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


class RunService:
    def __init__(self, storage: AnalysisStorage, lock: HostRunLock,
                 adapter_factory: Callable[[], CollectionSource], *, mode="live",
                 clean_snapshot, extract_keywords, score_trends, day_data_factory):
        if mode not in {"live", "synthetic", "replay"}:
            raise ValueError("Invalid run mode")
        self.storage = storage
        self.lock = lock
        self.adapter_factory = adapter_factory
        self.clean_snapshot = clean_snapshot
        self.extract_keywords = extract_keywords
        self.score_trends = score_trends
        self.day_data_factory = day_data_factory
        self.mode = mode
        self.tasks = set()

    async def recover(self):
        try:
            state = self.lock.acquire(ignore_cooldown=True)
        except RunBusy:
            return
        try:
            run_id = state.get("active_run_id")
            if run_id:
                run = await self.storage.get_run(run_id)
                if run and run.get("status") not in TERMINAL:
                    await self.storage.update_run(run_id, {"status": "ABORTED", "error": "Process interrupted; recovered after restart", "finished_at": now()})
                self.lock.write({"last_finished_at": now().isoformat()})
        finally:
            self.lock.release()

    async def start(self, *, execution_timeout_seconds=None):
        state = self.lock.acquire()
        try:
            if state.get("active_run_id"):
                old = await self.storage.get_run(state["active_run_id"])
                if old and old.get("status") not in TERMINAL:
                    await self.storage.update_run(state["active_run_id"], {"status": "ABORTED", "error": "Previous process interrupted", "finished_at": now()})
            run_id = uuid4().hex
            # Write marker first so a crash during creation never leaves an untracked run.
            self.lock.write({"active_run_id": run_id})
            await self.storage.create_run(run_id, {"run_id": run_id, "status": "QUEUED", "mode": self.mode, "created_at": now(), "source": "wikimedia_pageviews_top_per_country"})
            executor = (
                self._execute(run_id)
                if execution_timeout_seconds is None
                else self._execute_with_deadline(run_id, execution_timeout_seconds)
            )
            task = asyncio.create_task(executor)
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return run_id
        except BaseException:
            self.lock.release()
            raise

    async def _execute_with_deadline(self, run_id, timeout_seconds):
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("Invalid run execution deadline")
        try:
            await asyncio.wait_for(self._execute(run_id), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            try:
                await self.storage.update_run(
                    run_id,
                    {
                        "status": "ABORTED",
                        "finished_at": now(),
                        "error": "Configured execution deadline exceeded",
                    },
                )
            except Exception:
                # The host lock marker remains available for restart recovery.
                return

    async def _execute(self, run_id):
        writes = 0
        try:
            await self.storage.update_run(run_id, {"status": "FETCHING", "started_at": now()})
            collected = await self.adapter_factory().collect()
            if collected.analysis_date is None:
                failures = [{"date": snapshot.observation_date.isoformat(), "status": snapshot.fetch_status.value,
                             "http_status": snapshot.http_status, "error": snapshot.error}
                            for snapshot, _ in collected.days]
                for snapshot, _ in collected.days:
                    metadata = asdict(snapshot)
                    metadata["observation_date"] = snapshot.observation_date.isoformat()
                    metadata["fetch_status"] = snapshot.fetch_status.value
                    metadata["rejected_records"] = list(snapshot.rejected_records)
                    await self.storage.put_record(run_id, "snapshots", snapshot.observation_date.isoformat(), safe_doc(metadata))
                    writes += 1
                await self.storage.update_run(run_id, {"status": "FAILED", "finished_at": now(),
                    "error": "Wikimedia did not return an analysis date", "request_count": collected.request_count,
                    "coverage": failures, "snapshot_count": len(failures), "document_writes": writes})
                return
            await self.storage.update_run(run_id, {"status": "NORMALIZING", "data_date": collected.analysis_date.isoformat(), "request_count": collected.request_count})
            days, cleaning, total = [], [], 0
            for snapshot, rows in collected.days:
                clean = self.clean_snapshot(snapshot, rows)
                keywords = self.extract_keywords(clean.accepted)
                days.append(self.day_data_factory(snapshot, tuple(keywords), tuple(rows)))
                cleaning.append(clean)
                total += len(keywords)
            await self.storage.update_run(run_id, {"status": "ANALYZING", "observation_count": total})
            scores = self.score_trends(collected.analysis_date, days)
            current = {r.keyword_id: r for d in days if d.snapshot.observation_date == collected.analysis_date for r in d.keywords}
            if len(scores) != len(current):
                raise ValueError("Keyword and score counts do not reconcile")
            # Allow ten writes for run state and failure handling; abort before bulk writes.
            planned = len(days) + sum((len(day.keywords) + CHUNK_SIZE - 1) // CHUNK_SIZE for day in days) + SHARDS + 10
            if planned > MAX_RUN_WRITES:
                raise ValueError("Estimated document writes exceed run safety budget")
            await self.storage.update_run(run_id, {"status": "PERSISTING"})
            for day, cleaned in zip(days, cleaning):
                snapshot = day.snapshot
                report = cleaned.report
                metadata = asdict(snapshot)
                metadata["observation_date"] = snapshot.observation_date.isoformat()
                metadata["fetch_status"] = snapshot.fetch_status.value
                metadata["rejected_records"] = list(snapshot.rejected_records)
                metadata["cleaning"] = {"raw_count": report.raw_count, "accepted_count": report.accepted_count,
                    "rejected_count": report.rejected_count, "duplicate_count": report.duplicate_count,
                    "rejection_reasons": report.rejection_reasons, "quality_flags": list(report.quality_flags),
                    "rejected_records": [{"reason": item.reason, "observation": item.observation.to_canonical()} for item in cleaned.rejected],
                    "duplicates": [item.to_canonical() for item in cleaned.duplicates]}
                await self.storage.put_record(run_id, "snapshots", snapshot.observation_date.isoformat(), safe_doc(metadata))
                writes += 1
                # Each accepted observation is persisted once; chunking avoids thousands of document writes.
                for offset in range(0, len(day.keywords), CHUNK_SIZE):
                    data = {"date": snapshot.observation_date.isoformat(), "items": [item.to_record() for item in day.keywords[offset:offset+CHUNK_SIZE]]}
                    await self.storage.put_record(run_id, "observations", f"{snapshot.observation_date.isoformat()}-{offset // CHUNK_SIZE:04d}", safe_doc(data))
                    writes += 1
            buckets = [[] for _ in range(SHARDS)]
            for score in scores:
                row = current[score.keyword_id].to_record()
                row.update(score.to_record())
                buckets[int(score.keyword_id[0], 16)].append(row)
            for index, records in enumerate(buckets):
                await self.storage.put_record(run_id, "keyword_results", f"shard-{index:02d}", safe_doc({"items": records}))
                writes += 1
            history_start = collected.analysis_date - timedelta(days=7)
            coverage = [{"date": d.snapshot.observation_date.isoformat(), "status": d.snapshot.fetch_status.value,
                         "http_status": d.snapshot.http_status, "error": d.snapshot.error}
                        for d in days if history_start <= d.snapshot.observation_date <= collected.analysis_date]
            partial = any(item["status"] != FetchStatus.SUCCESS.value for item in coverage) or len(coverage) != 8
            await self.storage.update_run(run_id, {"status": "PARTIAL" if partial else "COMPLETED", "finished_at": now(),
                "fetched_at": max(d.snapshot.fetched_at for d in days), "observation_count": total,
                "keyword_count": len(scores), "scored_count": sum(s.score is not None for s in scores),
                "snapshot_count": len(days), "document_writes": writes, "coverage": coverage,
                "algorithm_version": "wikimedia-trend-v0.1", "classification_version": "title-rules-v0.1", "error": None})
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.storage.update_run(run_id, {"status": "ABORTED", "finished_at": now(), "error": "Process shutdown interrupted run"}))
            finally:
                raise
        except Exception as exc:
            try:
                await self.storage.update_run(run_id, {"status": "PARTIAL" if writes else "FAILED", "finished_at": now(),
                                                       "error": f"{type(exc).__name__}: {str(exc)[:300]}", "document_writes": writes})
            except Exception:
                # Marker remains for restart recovery if persistent storage is unavailable.
                return
        finally:
            # Keep the marker when the terminal update did not persist; recover() checks it.
            try:
                saved = await self.storage.get_run(run_id)
                if saved and saved.get("status") in TERMINAL:
                    self.lock.write({"last_finished_at": now().isoformat()})
            finally:
                self.lock.release()

    async def shutdown(self):
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
