"""Legacy analysis capture and isolated staging, including orphan records."""

import os
import uuid
from datetime import datetime, timezone

import pytest
from google.cloud.firestore_v1.types import Document, RunQueryResponse, Value
from google.protobuf.timestamp_pb2 import Timestamp
from psycopg import sql

from app.storage.analysis_document_codec import decode_document
from app.storage.firestore_analysis_to_postgres import (
    capture_firestore_analysis,
    stage_firestore_analysis,
)
from app.storage.firestore_to_postgres import LegacyCaptureError
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations

NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)
DATABASE = "projects/example/databases/(default)"


class Client:
    def __init__(self):
        stamp = Value(timestamp_value=Timestamp(seconds=1767225600, nanos=123_456_789))
        self.runs = {"run-1": {"created_at": stamp, "zero": Value(integer_value=0)}}
        self.records = {("orphan", "observations", "item"):
                        {"at": stamp, "missing": Value(null_value=0)}}

    def run_query(self, *, request, **_kwargs):
        selector = request["structured_query"]["from_"][0]
        kind = selector["collection_id"]
        read_time = Timestamp(seconds=1767225600, nanos=123456000)
        if kind == "analysis_runs" and not selector["all_descendants"]:
            docs = [("analysis_runs/" + key, data) for key, data in self.runs.items()]
        else:
            docs = [(f"analysis_runs/{run}/{item_kind}/{item}", data)
                    for (run, item_kind, item), data in self.records.items() if item_kind == kind]
        if "limit" in request["structured_query"]:
            docs = docs[:request["structured_query"]["limit"]]
        if not docs:
            return [RunQueryResponse(read_time=read_time)]
        return [RunQueryResponse(document=Document(name=DATABASE + "/documents/" + path, fields=data),
                                 read_time=read_time) for path, data in docs]


def test_changed_analysis_is_rejected_before_database_use():
    client = Client()
    capture = capture_firestore_analysis(client, database=DATABASE, now=NOW)
    client.runs["run-1"]["zero"] = Value(integer_value=1)
    with pytest.raises(LegacyCaptureError, match="changed after capture"):
        stage_firestore_analysis(client, {}, capture, database=DATABASE, schema="test")


def test_analysis_staging_preserves_timestamps_and_orphans_in_isolated_schema():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    client = Client()
    capture = capture_firestore_analysis(client, database=DATABASE, now=NOW)
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            created = True
            for _ in range(2):
                assert stage_firestore_analysis(client, environment, capture, database=DATABASE, schema=schema) == {
                    "runs": 1, "records": 1, "outside_group_records": 0}
            row = connection.execute(sql.SQL("SELECT payload,timestamp_paths FROM {}.analysis_records")
                                     .format(sql.Identifier(schema))).fetchone()
            decoded = decode_document(*row)
            assert decoded["at"].nanosecond == 123_456_789
            assert decoded["missing"] is None
            assert connection.execute(sql.SQL("SELECT run_id FROM {}.analysis_records")
                                      .format(sql.Identifier(schema))).fetchone()[0] == "orphan"
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
