"""Boundary for future source-specific retrieval and normalization."""

from datetime import date
from typing import Protocol

from app.models.observations import CollectionResult, DatasetSnapshot, PageObservation


class SourceAdapter(Protocol):
    async def fetch_day(
        self, observation_date: date
    ) -> tuple[DatasetSnapshot, list[PageObservation]]:
        """Fetch one day's snapshot; preserve unavailable data as missing."""
        ...


class CollectionSource(Protocol):
    """Collect snapshots, observations, errors and scope without provider SDK types."""

    async def collect(self) -> CollectionResult:
        ...
