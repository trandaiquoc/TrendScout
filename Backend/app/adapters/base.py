"""Boundary for future source-specific retrieval and normalization."""

from datetime import date
from typing import Protocol

from app.models.observations import DatasetSnapshot, PageObservation


class SourceAdapter(Protocol):
    async def fetch_day(
        self, observation_date: date
    ) -> tuple[DatasetSnapshot, list[PageObservation]]:
        """Fetch one day's snapshot; preserve unavailable data as missing."""
        ...
