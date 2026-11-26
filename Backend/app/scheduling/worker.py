"""Cancellable scheduler worker; execution remains an injected policy decision."""

import asyncio
import logging

from app.scheduling.common import ScheduleLeaseLost

_logger = logging.getLogger(__name__)


class ScheduleWorker:
    def __init__(self, store, run_schedule, *, poll_seconds: float = 5.0):
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

    def _finish(self, item, *, status, error_code=None):
        options = {"fence": item["fence"]} if "fence" in item else {}
        try:
            self.store.finish(item["occurrence_id"], status=status, error_code=error_code, **options)
        except ScheduleLeaseLost:
            _logger.warning("Scheduler occurrence lease lost before completion")

    async def _run(self):
        while not self._stop.is_set():
            item = await asyncio.to_thread(self.store.claim_due)
            if item is not None:
                try:
                    result = await self.run_schedule(item)
                    status = result if result in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED"} else "FAILED"
                    invalid = status == "FAILED" and result != "FAILED"
                    await asyncio.to_thread(self._finish, item, status=status,
                                            error_code="INVALID_RUN_RESULT" if invalid else None)
                except asyncio.CancelledError:
                    await asyncio.to_thread(self._finish, item, status="ABORTED", error_code="SHUTDOWN")
                    raise
                except Exception as exc:
                    await asyncio.to_thread(self._finish, item, status="FAILED", error_code=type(exc).__name__[:80])
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
