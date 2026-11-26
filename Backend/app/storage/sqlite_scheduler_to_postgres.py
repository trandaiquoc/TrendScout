"""Stage standalone SQLite schedule ledgers without activating a scheduler."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from psycopg import sql

from app.storage.postgres_migrations import _SCHEMA_NAME, verify_migrations
from app.storage.sqlite_to_postgres import (
    ImportConflict,
    InvalidBackup,
    _copy_batch,
    _source_digest,
    _stage_batch,
    _table_metadata,
)

_TABLES = ("schedules", "schedule_occurrences")


def _check_source(source):
    tables = {row[0] for row in source.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    if tables != set(_TABLES):
        raise InvalidBackup("source table inventory differs from supported scheduler schema")
    if (source.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or source.execute("PRAGMA foreign_key_check").fetchone() is not None):
        raise InvalidBackup("scheduler backup integrity check failed")
    if source.execute("SELECT 1 FROM schedule_occurrences WHERE status='RUNNING' LIMIT 1").fetchone():
        raise InvalidBackup("running scheduler occurrences require explicit reconciliation")


def inspect_scheduler_backup(connection, backup: Path, *, schema="trendscout", batch_size=500,
                             require_present=False):
    if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError("invalid PostgreSQL schema name")
    if not connection.autocommit or type(batch_size) is not int or not 1 <= batch_size <= 5000:
        raise ValueError("autocommit and bounded batch size required")
    verify_migrations(connection, schema=schema)
    path = Path(backup).absolute()
    digest, size = _source_digest(path)
    counts = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
        _check_source(source)
        for table in _TABLES:
            columns, keys, target = _table_metadata(connection, schema, source, table)
            counts[table] = source.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            order = ",".join('"' + key + '"' for key in keys)
            cursor = source.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
            while rows := cursor.fetchmany(batch_size):
                with connection.transaction():
                    connection.execute("SELECT set_config('statement_timeout', '120s', true)")
                    target_table, join, _ = _stage_batch(connection, schema, table, columns, keys, target, rows)
                    if require_present:
                        missing = sql.SQL("t.{} IS NULL").format(sql.Identifier(keys[0]))
                        query = sql.SQL("SELECT EXISTS (SELECT 1 FROM ts_import_batch s "
                                        "LEFT JOIN {} t ON {} WHERE {})")
                        if connection.execute(query.format(target_table, join, missing)).fetchone()[0]:
                            raise ImportConflict(f"source {table} rows are missing from PostgreSQL")
    if _source_digest(path) != (digest, size):
        raise InvalidBackup("scheduler backup changed during inspection")
    return {"source_sha256": digest, "size_bytes": size, "table_counts": counts}


def import_scheduler_backup(connection, backup: Path, *, schema="trendscout", batch_size=500):
    """Stage all non-running schedule rows, with durable resumable progress."""
    inspection = inspect_scheduler_backup(connection, backup, schema=schema, batch_size=batch_size)
    path = Path(backup).absolute()
    digest, size = inspection["source_sha256"], inspection["size_bytes"]
    initial_stat = path.stat()
    if _source_digest(path) != (digest, size):
        raise InvalidBackup("scheduler backup changed before import")
    after_digest = path.stat()
    if (initial_stat.st_size, initial_stat.st_mtime_ns) != (after_digest.st_size, after_digest.st_mtime_ns):
        raise InvalidBackup("scheduler backup changed before import")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
        _check_source(source)
        for table in _TABLES:
            columns, keys, target = _table_metadata(connection, schema, source, table)
            total = inspection["table_counts"][table]
            order = ",".join('"' + key + '"' for key in keys)
            while True:
                with connection.transaction():
                    connection.execute("SELECT set_config('lock_timeout', '10s', true)")
                    connection.execute("SELECT set_config('statement_timeout', '120s', true)")
                    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                                       ("trendscout-import:" + schema,))
                    connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
                    connection.execute("INSERT INTO import_sources(sha256,size_bytes) VALUES (%s,%s) "
                                       "ON CONFLICT DO NOTHING", (digest, size))
                    connection.execute("INSERT INTO import_progress(source_sha256,table_name) VALUES (%s,%s) "
                                       "ON CONFLICT DO NOTHING", (digest, table))
                    offset, completed = connection.execute("""SELECT rows_processed,completed
                        FROM import_progress WHERE source_sha256=%s AND table_name=%s FOR UPDATE""",
                        (digest, table)).fetchone()
                    if offset > total or (completed and offset != total):
                        raise InvalidBackup("scheduler import journal conflicts with backup")
                    if completed:
                        break
                    rows = source.execute(f'SELECT * FROM "{table}" ORDER BY {order} LIMIT ? OFFSET ?',
                                          (batch_size, offset)).fetchall()
                    if rows:
                        _copy_batch(connection, schema, table, columns, keys, target, rows)
                    if not rows and offset != total:
                        raise InvalidBackup("scheduler backup row count changed")
                    current = path.stat()
                    if (current.st_size, current.st_mtime_ns) != (initial_stat.st_size, initial_stat.st_mtime_ns):
                        raise InvalidBackup("scheduler backup changed during import")
                    offset += len(rows)
                    connection.execute("""UPDATE import_progress SET rows_processed=%s,completed=%s,
                        updated_at=now() WHERE source_sha256=%s AND table_name=%s""",
                        (offset, offset == total, digest, table))
                    if offset == total:
                        break
    if _source_digest(path) != (digest, size):
        raise InvalidBackup("scheduler backup changed during import")
    return inspect_scheduler_backup(connection, path, schema=schema, batch_size=batch_size,
                                    require_present=True)
