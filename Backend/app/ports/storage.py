"""Existing analysis persistence contract; document payloads are unchanged.

Implementations preserve omitted fields, explicit None, and numeric zero.
Records are stored in the existing per-run subcollections; this port does not
provide cascading deletion or claim a transaction across a complete run.
"""
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

Record = dict[str, Any]
RecordKind = Literal["snapshots", "observations", "keyword_results", "baseline_buckets"]


class StorageError(Exception):
    """A storage operation failed; the original exception is chained."""


class RecordNotFound(StorageError):
    """An update referenced a missing run."""


class RecordAlreadyExists(StorageError):
    """A run with the same identifier already exists."""


@runtime_checkable
class AnalysisStorage(Protocol):
    async def create_run(self, run_id: str, data: Mapping[str, Any]) -> None: ...
    async def get_run(self, run_id: str) -> Record | None: ...
    async def update_run(self, run_id: str, fields: Mapping[str, Any]) -> None: ...
    async def put_record(self, run_id: str, kind: RecordKind, record_id: str, data: Mapping[str, Any]) -> None: ...
    async def get_record(self, run_id: str, kind: RecordKind, record_id: str) -> Record | None: ...
    async def list_records(self, run_id: str, kind: RecordKind) -> list[Record]: ...
