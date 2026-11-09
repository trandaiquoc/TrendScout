"""Storage boundary; a Firestore implementation is reserved for step 3."""

from typing import Protocol

from app.models.observations import DatasetSnapshot, PageObservation


class ObservationStorage(Protocol):
    async def save_snapshot(
        self, snapshot: DatasetSnapshot, observations: list[PageObservation]
    ) -> None:
        """Persist a snapshot and its observed records."""
        ...


from typing import Any, Literal, Mapping

Record = dict[str, Any]
RecordKind = Literal["snapshots", "observations", "keyword_results", "baseline_buckets"]


class StorageError(Exception):
    """A storage operation failed; the original exception is chained."""


class RecordNotFound(StorageError):
    """An update referenced a missing run."""


class RecordAlreadyExists(StorageError):
    """A run with the same identifier already exists."""


class AnalysisStorage(Protocol):
    async def create_run(self, run_id: str, data: Mapping[str, Any]) -> None: ...
    async def get_run(self, run_id: str) -> Record | None: ...
    async def update_run(self, run_id: str, fields: Mapping[str, Any]) -> None: ...
    async def put_record(self, run_id: str, kind: RecordKind, record_id: str, data: Mapping[str, Any]) -> None: ...
    async def get_record(self, run_id: str, kind: RecordKind, record_id: str) -> Record | None: ...
