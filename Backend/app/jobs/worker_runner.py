"""Long-running consumer loop for a separately supervised worker process."""

from __future__ import annotations

import asyncio
import re

_OWNER = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


class DistributedWorkerRunner:
    """Poll a durable worker until shutdown; leave restart policy to the host.

    Queue ownership and recovery remain in ``DistributedFetchWorker`` and its
    atomic store. Unhandled storage or worker errors terminate this loop so an
    external process supervisor can restart it; this class never hides them.
    """

    def __init__(self, worker, *, owner: str, idle_poll_seconds: float = 1.0):
        if not callable(getattr(worker, "run_once", None)):
            raise TypeError("worker with run_once is required")
        if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
            raise ValueError("bounded worker owner is required")
        if (type(idle_poll_seconds) not in {int, float}
                or not 0.05 <= idle_poll_seconds <= 60):
            raise ValueError("idle poll interval must be between 0.05 and 60 seconds")
        self.worker = worker
        self.owner = owner
        self.idle_poll_seconds = float(idle_poll_seconds)

    async def run(self, stop_event: asyncio.Event) -> int:
        """Consume jobs until signaled; return the count of claimed outcomes."""
        if not isinstance(stop_event, asyncio.Event):
            raise TypeError("asyncio shutdown event is required")
        completed = 0
        while not stop_event.is_set():
            outcome = await self.worker.run_once(self.owner)
            if outcome is not None:
                if not isinstance(outcome, dict):
                    raise TypeError("worker outcome must be an object")
                completed += 1
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.idle_poll_seconds
                )
            except TimeoutError:
                pass
        return completed
