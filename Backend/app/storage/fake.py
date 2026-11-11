"""In-memory test repository; contents never persist across processes."""

from copy import deepcopy
from typing import Any, Mapping

from app.ports.storage import Record, RecordAlreadyExists, RecordKind, RecordNotFound
from app.storage.common import validate_fields, validate_id, validate_kind


class FakeStorage:
    __trendscout_offline_only__ = True

    def __init__(self) -> None:
        self._runs: dict[str, Record] = {}
        self._records: dict[tuple[str, str, str], Record] = {}

    async def create_run(self, run_id: str, data: Mapping[str, Any]) -> None:
        validate_id(run_id)
        validate_fields(data)
        if run_id in self._runs:
            raise RecordAlreadyExists("Record already exists")
        self._runs[run_id] = deepcopy(dict(data))

    async def get_run(self, run_id: str) -> Record | None:
        validate_id(run_id)
        return deepcopy(self._runs.get(run_id))

    async def update_run(self, run_id: str, fields: Mapping[str, Any]) -> None:
        validate_id(run_id)
        validate_fields(fields, update=True)
        if run_id not in self._runs:
            raise RecordNotFound("Record not found")
        self._runs[run_id].update(deepcopy(dict(fields)))

    async def put_record(self, run_id: str, kind: RecordKind, record_id: str, data: Mapping[str, Any]) -> None:
        validate_id(run_id)
        validate_id(record_id)
        validate_kind(kind)
        validate_fields(data)
        self._records[run_id, kind, record_id] = deepcopy(dict(data))

    async def get_record(self, run_id: str, kind: RecordKind, record_id: str) -> Record | None:
        validate_id(run_id)
        validate_id(record_id)
        validate_kind(kind)
        return deepcopy(self._records.get((run_id, kind, record_id)))

    async def list_records(self, run_id: str, kind: RecordKind) -> list[Record]:
        validate_id(run_id)
        validate_kind(kind)
        return [deepcopy(value) for (run, category, _), value in sorted(self._records.items()) if run == run_id and category == kind]
