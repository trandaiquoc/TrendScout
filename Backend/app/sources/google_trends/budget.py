"""Per-attempt SerpApi account reconciliation; no paid request if it fails."""

from __future__ import annotations

import asyncio

from app.sources.serpapi.account import SerpApiAccountClient
from app.sources.serpapi.errors import SerpApiError


class SerpApiAccountBudget:
    """Refresh provider quota metadata before each physical paid attempt.

    The safety reserve reduces cross-machine races but cannot create a globally
    atomic ledger across independent local SQLite files.
    """

    def __init__(self, client: SerpApiAccountClient, *, cross_machine_reserve: int,
                 on_snapshot=None):
        if (not isinstance(client, SerpApiAccountClient)
                or type(cross_machine_reserve) is not int
                or not 0 <= cross_machine_reserve <= 1000
                or on_snapshot is not None and not callable(on_snapshot)):
            raise ValueError("account client and bounded cross-machine reserve required")
        self.client = client
        self.cross_machine_reserve = cross_machine_reserve
        self.on_snapshot = on_snapshot
        self._lock = asyncio.Lock()

    async def ensure_attempt_allowed(self) -> dict:
        async with self._lock:
            try:
                snapshot = await self.client.fetch()
            except SerpApiError:
                self._publish({"budget_status": "UNAVAILABLE"})
                raise
            public = snapshot.public_metadata()
            remaining = snapshot.total_remaining
            if snapshot.monthly_remaining is not None:
                remaining = min(remaining, snapshot.monthly_remaining)
            if (remaining <= self.cross_machine_reserve
                    or snapshot.current_hour_searches >= snapshot.hourly_limit):
                self._publish({**public, "budget_status": "BUDGET_EXHAUSTED"})
                raise SerpApiError("QUOTA_BUDGET_EXHAUSTED")
            checked = {**public, "budget_status": "AVAILABLE"}
            self._publish(checked)
            return checked

    def _publish(self, snapshot):
        if self.on_snapshot is not None:
            try:
                self.on_snapshot(snapshot)
            except SerpApiError:
                raise
            except Exception:
                raise SerpApiError("QUOTA_STATE_UNAVAILABLE") from None
