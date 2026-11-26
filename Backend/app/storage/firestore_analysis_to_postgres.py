"""Read and stage legacy analysis documents without changing runtime authority."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.cloud.firestore_v1.types import RunQueryResponse
from google.protobuf.timestamp_pb2 import Timestamp
from psycopg import sql
from psycopg.types.json import Jsonb

from app.storage.analysis_document_codec import (
    analysis_record_key,
    created_sort_key,
    encode_document,
)
from app.storage.common import KINDS, validate_id
from app.storage.firestore_to_postgres import LegacyCaptureError, _scan_sensitive
from app.storage.postgres_atomic_state import _same
from app.storage.postgres_config import verify_same_database
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import verify_migrations


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LegacyCaptureError("timezone-aware capture time required")
    return value.astimezone(timezone.utc)


def _digest(encoded):
    try:
        raw = json.dumps(encoded, ensure_ascii=False, sort_keys=True, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise LegacyCaptureError("analysis document cannot be encoded safely") from None
    return hashlib.sha256(raw).hexdigest()


def _decode_value(value):
    kind = value.WhichOneof("value_type")
    if kind == "null_value":
        return None
    if kind in {"boolean_value", "integer_value", "double_value", "string_value"}:
        return getattr(value, kind)
    if kind == "timestamp_value":
        stamp = value.timestamp_value
        try:
            base = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=stamp.seconds)
            return DatetimeWithNanoseconds(base.year, base.month, base.day, base.hour, base.minute,
                                           base.second, nanosecond=stamp.nanos, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            raise LegacyCaptureError("analysis timestamp is invalid") from None
    if kind == "array_value":
        return [_decode_value(child) for child in value.array_value.values]
    if kind == "map_value":
        return {key: _decode_value(child) for key, child in value.map_value.fields.items()}
    raise LegacyCaptureError("analysis document contains an unsupported Firestore value")


def _encoded(document):
    data = {key: _decode_value(value) for key, value in document.fields.items()}
    _scan_sensitive(data)
    try:
        return encode_document(data)
    except ValueError:
        raise LegacyCaptureError("analysis document contains unsupported data") from None


def _read_analysis(client, database):
    if (not isinstance(database, str)
            or not re.fullmatch(r"projects/[^/]+/databases/[^/]+", database)):
        raise ValueError("explicit Firestore database resource required")
    parent = database + "/documents"

    def query(collection, *, descendants=False, read_time=None, limit=None):
        structured = {"from_": [{"collection_id": collection, "all_descendants": descendants}]}
        if limit is not None:
            structured["limit"] = limit
        request = {"parent": parent, "structured_query": structured}
        if read_time is not None:
            request["read_time"] = read_time
        return client.run_query(request=request, timeout=60, retry=None)

    # Obtain a server timestamp first, then use one microsecond-precision read
    # time for every query, including the root query. High-level to_dict() uses
    # deepcopy, which can discard DatetimeWithNanoseconds' submicroseconds.
    read_time = None
    for response in query("analysis_runs", limit=1):
        raw_response = RunQueryResponse.pb(response)
        if raw_response.HasField("read_time"):
            stamp = raw_response.read_time
            read_time = Timestamp(seconds=stamp.seconds, nanos=(stamp.nanos // 1000) * 1000)
            break
    if read_time is None:
        raise LegacyCaptureError("Firestore did not provide a consistent read time")
    documents = {}
    prefix = parent + "/"
    for response in query("analysis_runs", read_time=read_time):
        document = RunQueryResponse.pb(response).document
        if not document.name:
            continue
        if not document.name.startswith(prefix):
            raise LegacyCaptureError("analysis document database path is invalid")
        path = document.name[len(prefix):].split("/")
        if len(path) != 2 or path[0] != "analysis_runs":
            raise LegacyCaptureError("analysis run path is invalid")
        validate_id(path[1])
        key = (path[1], None, None)
        if key in documents:
            raise LegacyCaptureError("duplicate analysis run")
        documents[key] = _encoded(document)
    outside = 0
    for kind in sorted(KINDS):
        for response in query(kind, descendants=True, read_time=read_time):
            document = RunQueryResponse.pb(response).document
            if not document.name:
                continue
            if not document.name.startswith(prefix):
                raise LegacyCaptureError("analysis document database path is invalid")
            path = document.name[len(prefix):].split("/")
            if len(path) != 4 or path[0] != "analysis_runs" or path[2] != kind:
                outside += 1
                continue
            run_id, record_id = path[1], path[3]
            validate_id(run_id)
            validate_id(record_id)
            key = (run_id, kind, record_id)
            if key in documents:
                raise LegacyCaptureError("duplicate analysis record")
            documents[key] = _encoded(document)
    return documents, outside


@dataclass(frozen=True)
class AnalysisCapture:
    captured_at: datetime
    documents: dict = field(repr=False)
    fingerprints: dict = field(repr=False)
    outside_group_records: int = 0


def capture_firestore_analysis(client, *, database, now):
    """Capture all known analysis-run document kinds, including orphan records."""
    documents, outside = _read_analysis(client, database)
    return AnalysisCapture(_utc(now), deepcopy(documents),
                           {key: _digest(value) for key, value in documents.items()}, outside)


def _write_batch(connection, schema, items, captured_at):
    runs = [(key, value) for key, value in items if key[1] is None]
    records = [(key, value) for key, value in items if key[1] is not None]
    run_table = sql.SQL("{}.analysis_runs").format(sql.Identifier(schema))
    record_table = sql.SQL("{}.analysis_records").format(sql.Identifier(schema))
    audit_table = sql.SQL("{}.legacy_firestore_imports").format(sql.Identifier(schema))
    with connection.transaction():
        connection.execute("SELECT set_config('statement_timeout', '120s', true)")
        with connection.cursor() as cursor:
            if runs:
                cursor.executemany(sql.SQL("""INSERT INTO {}(run_id,payload,timestamp_paths,
                    created_kind,created_number,created_seconds,created_nanos,created_text)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""").format(run_table),
                    [(key[0], Jsonb(value[0]), Jsonb(value[1]), *created_sort_key(*value))
                     for key, value in runs])
            if records:
                cursor.executemany(sql.SQL("""INSERT INTO {}(record_key,run_id,kind,record_id,payload,timestamp_paths)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""").format(record_table),
                    [(analysis_record_key(*key), *key, Jsonb(value[0]), Jsonb(value[1])) for key, value in records])
            audit = [("analysis:" + analysis_record_key(key[0], key[1] or "", key[2] or ""), _digest(value))
                     for key, value in items]
            cursor.executemany(sql.SQL("""INSERT INTO {}(document_id,content_sha256,disposition,captured_at)
                VALUES (%s,%s,'ANALYSIS',%s) ON CONFLICT DO NOTHING""").format(audit_table),
                [(audit_id, digest, captured_at) for audit_id, digest in audit])
            audit_rows = cursor.execute(sql.SQL("""SELECT document_id,content_sha256,disposition
                FROM {} WHERE document_id=ANY(%s)""").format(audit_table),
                ([audit_id for audit_id, _ in audit],)).fetchall()
            audit_found = {row[0]: (row[1], row[2]) for row in audit_rows}
            if any(audit_found.get(audit_id) != (digest, "ANALYSIS") for audit_id, digest in audit):
                raise LegacyCaptureError("analysis import audit conflicts with staged data")
            if runs:
                rows = cursor.execute(sql.SQL("""SELECT run_id,payload,timestamp_paths,created_kind,
                    created_number,created_seconds,created_nanos,created_text FROM {} WHERE run_id=ANY(%s)""")
                    .format(run_table), ([key[0] for key, _ in runs],)).fetchall()
                found = {row[0]: ((row[1], row[2]), tuple(row[3:])) for row in rows}
                if any(key[0] not in found or not _same(found[key[0]][0], value)
                       or found[key[0]][1] != created_sort_key(*value) for key, value in runs):
                    raise LegacyCaptureError("analysis run conflicts with staged data")
            if records:
                keys = [analysis_record_key(*key) for key, _ in records]
                rows = cursor.execute(sql.SQL("""SELECT record_key,run_id,kind,record_id,payload,timestamp_paths
                    FROM {} WHERE record_key=ANY(%s)""").format(record_table), (keys,)).fetchall()
                found = {row[0]: (tuple(row[1:4]), (row[4], row[5])) for row in rows}
                if any(analysis_record_key(*key) not in found or found[analysis_record_key(*key)][0] != key
                       or not _same(found[analysis_record_key(*key)][1], value) for key, value in records):
                    raise LegacyCaptureError("analysis record conflicts with staged data")


def stage_firestore_analysis(client, environment, capture, *, database, schema, batch_size=100):
    """Stage and reconcile a captured analysis namespace, without cutover."""
    if not isinstance(capture, AnalysisCapture) or type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("valid capture and bounded batch size required")
    before, outside = _read_analysis(client, database)
    expected = {key: _digest(value) for key, value in before.items()}
    if (expected != capture.fingerprints or outside != capture.outside_group_records
            or {key: _digest(value) for key, value in capture.documents.items()} != capture.fingerprints):
        raise LegacyCaptureError("legacy analysis documents changed after capture")
    verify_same_database(environment)
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        verify_migrations(connection, schema=schema)
        items = sorted(capture.documents.items(), key=lambda item: tuple(part or "" for part in item[0]))
        for offset in range(0, len(items), batch_size):
            _write_batch(connection, schema, items[offset:offset + batch_size], capture.captured_at)
        after, outside = _read_analysis(client, database)
        if ({key: _digest(value) for key, value in after.items()} != capture.fingerprints
                or outside != capture.outside_group_records):
            raise LegacyCaptureError("legacy analysis documents changed during staging")
        # Recheck stored contents even on reruns where every insert conflicted.
        for offset in range(0, len(items), batch_size):
            _write_batch(connection, schema, items[offset:offset + batch_size], capture.captured_at)
    return {"runs": sum(key[1] is None for key in capture.documents),
            "records": sum(key[1] is not None for key in capture.documents),
            "outside_group_records": capture.outside_group_records}
