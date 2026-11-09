"""Storage boundary; a Firestore implementation is reserved for step 3."""

from typing import Protocol

from app.models.observations import DatasetSnapshot, PageObservation


class ObservationStorage(Protocol):
    async def save_snapshot(
        self, snapshot: DatasetSnapshot, observations: list[PageObservation]
    ) -> None:
        """Persist a snapshot and its observed records."""
        ...
