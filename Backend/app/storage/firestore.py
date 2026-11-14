"""Firestore storage using Firebase Admin SDK and Application Default Credentials."""

import asyncio
from typing import Any, Mapping
from uuid import uuid4

import firebase_admin
from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists, GoogleAPICallError, NotFound
from google.auth.exceptions import GoogleAuthError

from app.ports.storage import Record, RecordAlreadyExists, RecordKind, RecordNotFound, StorageError
from app.settings import Settings
from app.storage.common import validate_fields, validate_id, validate_kind


class FirestoreStorage:
    def __init__(self, client: Any):
        self._client = client

    @classmethod
    def from_environment(cls) -> "FirestoreStorage":
        project_id = Settings.from_environment().google_cloud_project or ""
        return cls.from_project(project_id)

    @classmethod
    def from_project(cls, project_id: str) -> "FirestoreStorage":
        if not project_id or not project_id.strip():
            raise ValueError("GOOGLE_CLOUD_PROJECT must be set")
        # A distinct named Firebase app avoids silently reusing another project's client.
        app = firebase_admin.initialize_app(options={"projectId": project_id}, name=f"trendscout-{uuid4().hex}")
        try:
            return cls(firestore.client(app=app, database_id="(default)"))
        except Exception:
            firebase_admin.delete_app(app)
            raise

    def _run(self, run_id: str) -> Any:
        validate_id(run_id)
        return self._client.collection("analysis_runs").document(run_id)

    def _record(self, run_id: str, kind: RecordKind, record_id: str) -> Any:
        validate_kind(kind)
        validate_id(record_id)
        return self._run(run_id).collection(kind).document(record_id)

    async def _call(self, function: Any, *args: Any) -> Any:
        try:
            return await asyncio.to_thread(function, *args)
        except AlreadyExists as exc:
            raise RecordAlreadyExists("Record already exists") from exc
        except NotFound as exc:
            raise RecordNotFound("Record not found") from exc
        except (GoogleAPICallError, GoogleAuthError) as exc:
            raise StorageError("Firestore operation failed") from exc

    async def create_run(self, run_id: str, data: Mapping[str, Any]) -> None:
        validate_fields(data)
        await self._call(self._run(run_id).create, dict(data))

    async def get_run(self, run_id: str) -> Record | None:
        snapshot = await self._call(self._run(run_id).get)
        return snapshot.to_dict() if snapshot.exists else None

    async def list_runs(self, *, limit: int = 100, offset: int = 0) -> list[Record]:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 500:
            raise ValueError("Invalid run history page")
        query = (
            self._client.collection("analysis_runs")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .offset(offset)
            .limit(limit)
        )
        return await self._call(lambda: [snapshot.to_dict() for snapshot in query.stream()])

    async def list_runs_after(self, *, limit: int = 100, after_run_id: str | None = None) -> list[Record]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid run history page")
        collection = self._client.collection("analysis_runs")
        query = collection.order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
        if after_run_id is not None:
            validate_id(after_run_id)
            snapshot = await self._call(collection.document(after_run_id).get)
            if not snapshot.exists:
                raise ValueError("Invalid run history cursor")
            query = query.start_after(snapshot)
        def read_page():
            page = []
            for snapshot in query.stream():
                record = snapshot.to_dict() or {}
                record.setdefault("run_id", snapshot.id)
                page.append(record)
            return page
        return await self._call(read_page)

    async def update_run(self, run_id: str, fields: Mapping[str, Any]) -> None:
        validate_fields(fields, update=True)
        await self._call(self._run(run_id).update, dict(fields))

    async def put_record(self, run_id: str, kind: RecordKind, record_id: str, data: Mapping[str, Any]) -> None:
        validate_fields(data)
        await self._call(self._record(run_id, kind, record_id).set, dict(data))

    async def get_record(self, run_id: str, kind: RecordKind, record_id: str) -> Record | None:
        snapshot = await self._call(self._record(run_id, kind, record_id).get)
        return snapshot.to_dict() if snapshot.exists else None

    async def list_records(self, run_id: str, kind: RecordKind) -> list[Record]:
        validate_kind(kind)
        collection = self._run(run_id).collection(kind)
        return await self._call(lambda: [doc.to_dict() for doc in collection.stream()])
