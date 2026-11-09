"""Backward-compatible storage contracts; application ports live in app.ports."""
from typing import Protocol

from app.models.observations import DatasetSnapshot, PageObservation
from app.ports.storage import (
    AnalysisStorage, Record, RecordAlreadyExists, RecordKind, RecordNotFound, StorageError,
)


class ObservationStorage(Protocol):
    async def save_snapshot(
        self, snapshot: DatasetSnapshot, observations: list[PageObservation]
    ) -> None:
        """Legacy prospective interface retained for import compatibility."""
        ...
