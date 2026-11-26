"""Native PostgreSQL implementation of the existing analysis storage port.

It is intentionally not wired into runtime until migration and fencing gates
are met. Each operation uses a bounded, short transaction and makes no
provider requests.
"""

from __future__ import annotations

import asyncio

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from app.ports.storage import RecordAlreadyExists, RecordNotFound, StorageError
from app.storage.analysis_document_codec import (
    analysis_record_key,
    created_sort_key,
    decode_document,
    encode_document,
)
from app.storage.common import validate_fields, validate_id, validate_kind
from app.storage.document_security import validate_no_credentials
from app.storage.postgres_connection import PostgresConnectionError, postgres_connection
from app.storage.postgres_migrations import _SCHEMA_NAME

_SORT = ('created_kind DESC, created_number DESC, created_seconds DESC, '
         'created_nanos DESC, created_text DESC, run_id COLLATE "C" DESC')
_SORT_TUPLE = ('(created_kind,created_number,created_seconds,created_nanos,created_text,'
               'run_id COLLATE "C")')


def _encoded(data):
    value = dict(data)
    validate_no_credentials(value)
    return encode_document(value)


class PostgresAnalysisStorage:
    def __init__(self, environment, *, schema="trendscout"):
        if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError("invalid PostgreSQL schema name")
        self._environment = dict(environment)
        self._schema = schema

    def _table(self, name):
        return sql.SQL("{}.{}").format(sql.Identifier(self._schema), sql.Identifier(name))

    def _transaction(self, operation, *, read_only=False):
        try:
            with postgres_connection(self._environment) as connection:
                with connection.transaction():
                    if read_only:
                        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    connection.execute("SELECT set_config('statement_timeout', '30s', true)")
                    connection.execute("SELECT set_config('lock_timeout', '10s', true)")
                    connection.execute("SELECT set_config('idle_in_transaction_session_timeout', '30s', true)")
                    return operation(connection)
        except psycopg.errors.UniqueViolation:
            raise RecordAlreadyExists("Record already exists") from None
        except (psycopg.Error, PostgresConnectionError) as exc:
            state = getattr(exc, "sqlstate", None)
            raise StorageError(f"PostgreSQL operation failed ({state or 'connection'})") from None

    async def _call(self, operation, *, read_only=False):
        return await asyncio.to_thread(self._transaction, operation, read_only=read_only)

    async def create_run(self, run_id, data):
        validate_id(run_id)
        validate_fields(data)
        payload, timestamps = _encoded(data)
        def create(db):
            db.execute(sql.SQL("""INSERT INTO {}(run_id,payload,timestamp_paths,created_kind,created_number,
                created_seconds,created_nanos,created_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""")
                .format(self._table("analysis_runs")),
                (run_id, Jsonb(payload), Jsonb(timestamps), *created_sort_key(payload, timestamps)))
        await self._call(create)

    async def get_run(self, run_id):
        validate_id(run_id)
        def get(db):
            row = db.execute(sql.SQL("SELECT payload,timestamp_paths FROM {} WHERE run_id=%s")
                             .format(self._table("analysis_runs")), (run_id,)).fetchone()
            return decode_document(*row) if row else None
        return await self._call(get, read_only=True)

    def _check_sortable(self, db):
        if db.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {} WHERE created_kind=6)")
                      .format(self._table("analysis_runs"))).fetchone()[0]:
            raise StorageError("Run history contains an unsupported created_at type")

    async def list_runs(self, *, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 500:
            raise ValueError("Invalid run history page")
        def listing(db):
            self._check_sortable(db)
            query = sql.SQL("SELECT payload,timestamp_paths FROM {} WHERE created_kind IS NOT NULL ORDER BY "
                            + _SORT + " LIMIT %s OFFSET %s").format(self._table("analysis_runs"))
            return [decode_document(*row) for row in db.execute(query, (limit, offset)).fetchall()]
        return await self._call(listing, read_only=True)

    async def list_runs_after(self, *, limit=100, after_run_id=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid run history page")
        if after_run_id is not None:
            validate_id(after_run_id)
        def listing(db):
            self._check_sortable(db)
            conditions = "created_kind IS NOT NULL"
            parameters = []
            if after_run_id is not None:
                cursor = db.execute(sql.SQL("""SELECT created_kind,created_number,created_seconds,
                    created_nanos,created_text,run_id FROM {} WHERE run_id=%s""")
                    .format(self._table("analysis_runs")), (after_run_id,)).fetchone()
                if cursor is None or cursor[0] is None or cursor[0] == 6:
                    raise ValueError("Invalid run history cursor")
                conditions += (" AND " + _SORT_TUPLE
                               + ' < (%s,%s,%s,%s,%s,CAST(%s AS text) COLLATE "C")')
                parameters.extend(cursor)
            parameters.append(limit)
            query = sql.SQL("SELECT run_id,payload,timestamp_paths FROM {} WHERE " + conditions
                            + " ORDER BY " + _SORT + " LIMIT %s").format(self._table("analysis_runs"))
            page = []
            for run_id, payload, timestamps in db.execute(query, parameters).fetchall():
                record = decode_document(payload, timestamps)
                record.setdefault("run_id", run_id)
                page.append(record)
            return page
        return await self._call(listing, read_only=True)

    async def update_run(self, run_id, fields):
        validate_id(run_id)
        validate_fields(fields, update=True)
        incoming = _encoded(fields)
        def update(db):
            row = db.execute(sql.SQL("SELECT payload,timestamp_paths FROM {} WHERE run_id=%s FOR UPDATE")
                             .format(self._table("analysis_runs")), (run_id,)).fetchone()
            if row is None:
                raise RecordNotFound("Record not found")
            value = decode_document(*row)
            value.update(decode_document(*incoming))
            payload, timestamps = encode_document(value)
            db.execute(sql.SQL("""UPDATE {} SET payload=%s,timestamp_paths=%s,created_kind=%s,
                created_number=%s,created_seconds=%s,created_nanos=%s,created_text=%s WHERE run_id=%s""")
                .format(self._table("analysis_runs")),
                (Jsonb(payload), Jsonb(timestamps), *created_sort_key(payload, timestamps), run_id))
        await self._call(update)

    async def put_record(self, run_id, kind, record_id, data):
        validate_id(run_id)
        validate_id(record_id)
        validate_kind(kind)
        validate_fields(data)
        payload, timestamps = _encoded(data)
        key = analysis_record_key(run_id, kind, record_id)
        def put(db):
            row = db.execute(sql.SQL("""INSERT INTO {} AS target
                (record_key,run_id,kind,record_id,payload,timestamp_paths) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(record_key) DO UPDATE SET payload=EXCLUDED.payload,timestamp_paths=EXCLUDED.timestamp_paths
                WHERE (target.run_id,target.kind,target.record_id)=(EXCLUDED.run_id,EXCLUDED.kind,EXCLUDED.record_id)
                RETURNING record_key""").format(self._table("analysis_records")),
                (key, run_id, kind, record_id, Jsonb(payload), Jsonb(timestamps))).fetchone()
            if row is None:
                raise StorageError("Analysis record identity collision")
        await self._call(put)

    async def get_record(self, run_id, kind, record_id):
        validate_id(run_id)
        validate_id(record_id)
        validate_kind(kind)
        key = analysis_record_key(run_id, kind, record_id)
        def get(db):
            row = db.execute(sql.SQL("SELECT run_id,kind,record_id,payload,timestamp_paths FROM {} WHERE record_key=%s")
                             .format(self._table("analysis_records")), (key,)).fetchone()
            if row is None:
                return None
            if tuple(row[:3]) != (run_id, kind, record_id):
                raise StorageError("Analysis record identity collision")
            return decode_document(row[3], row[4])
        return await self._call(get, read_only=True)

    async def list_records(self, run_id, kind):
        validate_id(run_id)
        validate_kind(kind)
        def listing(db):
            query = sql.SQL('SELECT payload,timestamp_paths FROM {} WHERE run_id=%s AND kind=%s '
                            'ORDER BY record_id COLLATE "C"').format(self._table("analysis_records"))
            return [decode_document(*row) for row in db.execute(query, (run_id, kind)).fetchall()]
        return await self._call(listing, read_only=True)
