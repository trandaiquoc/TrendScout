"""Import an immutable SQLite Collections backup into native PostgreSQL rows.

Explicitly invoked migration utility, not a runtime storage backend. Import
requires legacy writers to be fenced before final reconciliation. Each batch
and its progress marker commit together; existing different rows are rejected.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from psycopg import sql

from app.storage.postgres_migrations import _SCHEMA_NAME, verify_migrations

TABLES = (
    "collections", "keywords", "memberships", "feedback", "cache", "proposals",
    "suppression", "settings", "fetch_jobs", "algorithm_observations", "algorithm_results",
    "dataset_snapshots", "keyword_decisions", "entity_context", "classification_results",
    "membership_reconciliation_runs", "membership_reconciliation_items", "research_records",
)
_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class ImportConflict(RuntimeError):
    """Existing PostgreSQL data differs from the source or violates a unique key."""


class InvalidBackup(ValueError):
    """The backup cannot be imported without losing or guessing data."""


def _identifier(name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise InvalidBackup("invalid database identifier")
    return name


def _source_digest(path: Path) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise InvalidBackup("source must be an immutable regular backup file")
    # An immutable reader cannot see WAL records. Require a standalone backup,
    # not a live database whose apparent main file may omit committed changes.
    if Path(str(path) + "-wal").exists():
        raise InvalidBackup("source has a WAL sidecar; create a consistent standalone backup")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise InvalidBackup("source backup changed while being read")
    return digest.hexdigest(), after.st_size


def _check_source(source):
    tables = {row[0] for row in source.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    if tables != set(TABLES):
        raise InvalidBackup("source table inventory differs from supported Collections schema")
    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise InvalidBackup("source integrity check failed")
    if source.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InvalidBackup("source has foreign key violations")


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidBackup("JSON contains duplicate object keys")
        result[key] = value
    return result


def _bad_constant(_value):
    raise InvalidBackup("JSON contains a non-finite number")


def _convert(value, data_type):
    if value is None:
        return None
    if data_type == "jsonb":
        try:
            json.loads(value, object_pairs_hook=_object_pairs, parse_constant=_bad_constant)
        except (TypeError, ValueError) as exc:
            raise InvalidBackup("source contains invalid JSON") from exc
        # COPY parses this exact JSON text as JSONB. Re-encoding a Python float
        # could silently round a provider's original decimal value.
        return value
    if data_type == "timestamp with time zone":
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise InvalidBackup("source contains an invalid timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise InvalidBackup("source timestamp lacks timezone; refusing to invent one")
        return parsed
    return value


def _table_metadata(connection, schema, source, table):
    source_info = source.execute(f'PRAGMA table_info("{_identifier(table)}")').fetchall()
    columns = [_identifier(row[1]) for row in source_info]
    keys = [row[1] for row in sorted((row for row in source_info if row[5]), key=lambda row: row[5])]
    target_info = connection.execute("""SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns WHERE table_schema=%s AND table_name=%s
        ORDER BY ordinal_position""", (schema, table)).fetchall()
    target = {row[0]: row for row in target_info}
    if (not keys or not target or set(columns) - set(target)
            or any(name not in columns and row[2] == "NO" and row[3] is None
                   for name, row in target.items())):
        raise InvalidBackup("source and target column inventories are incompatible")
    target_keys = [row[0] for row in connection.execute("""SELECT a.attname
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum
        WHERE n.nspname=%s AND t.relname=%s AND c.contype='p' ORDER BY k.ord""",
        (schema, table)).fetchall()]
    if keys != target_keys:
        raise InvalidBackup("source and target primary keys differ")
    return columns, keys, target


def _collection_rows(source, columns):
    """Order parents before children across committed batches."""
    rows = source.execute('SELECT * FROM "collections" ORDER BY id').fetchall()
    id_index, parent_index = columns.index("id"), columns.index("parent_id")
    by_id = {row[id_index]: row for row in rows}
    ordered, visited, visiting = [], set(), set()

    def visit(key):
        if key in visited:
            return
        if key in visiting or key not in by_id:
            raise InvalidBackup("collection hierarchy has a cycle or missing parent")
        visiting.add(key)
        parent = by_id[key][parent_index]
        if parent is not None:
            visit(parent)
        visiting.remove(key)
        visited.add(key)
        ordered.append(by_id[key])

    for key in sorted(by_id):
        visit(key)
    return ordered


def _stage_batch(connection, schema, table, columns, keys, target, rows):
    target_table = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    connection.execute(sql.SQL("CREATE TEMP TABLE ts_import_batch (LIKE {} INCLUDING DEFAULTS) "
                               "ON COMMIT DROP").format(target_table))
    column_names = sql.SQL(", ").join(map(sql.Identifier, columns))
    with connection.cursor().copy(sql.SQL("COPY ts_import_batch ({}) FROM STDIN").format(column_names)) as copy:
        for row in rows:
            copy.write_row([_convert(value, target[name][1]) for name, value in zip(columns, row)])
    join = sql.SQL(" AND ").join(sql.SQL("t.{0} = s.{0}").format(sql.Identifier(key)) for key in keys)
    all_columns = list(target)
    target_row = sql.SQL(", ").join(sql.SQL("t.{}").format(sql.Identifier(name)) for name in all_columns)
    source_row = sql.SQL(", ").join(sql.SQL("s.{}").format(sql.Identifier(name)) for name in all_columns)
    mismatch = sql.SQL("ROW({}) IS DISTINCT FROM ROW({})").format(target_row, source_row)
    query = sql.SQL("SELECT EXISTS (SELECT 1 FROM ts_import_batch s JOIN {} t ON {} WHERE {})")
    if connection.execute(query.format(target_table, join, mismatch)).fetchone()[0]:
        raise ImportConflict(f"existing {table} rows differ from the source")
    if table == "collections" and connection.execute(sql.SQL("""SELECT EXISTS (
        SELECT 1 FROM ts_import_batch s JOIN {} t ON s.category_id=t.category_id
        WHERE s.category_id IS NOT NULL AND s.id<>t.id)""").format(target_table)).fetchone()[0]:
        raise ImportConflict("collection category conflicts with existing PostgreSQL data")
    if table == "schedule_occurrences" and connection.execute(sql.SQL("""SELECT EXISTS (
        SELECT 1 FROM ts_import_batch s JOIN {} t
        ON s.schedule_id=t.schedule_id AND s.scheduled_at=t.scheduled_at WHERE s.id<>t.id)""")
        .format(target_table)).fetchone()[0]:
        raise ImportConflict("schedule occurrence conflicts with existing PostgreSQL data")
    return target_table, join, mismatch


def _copy_batch(connection, schema, table, columns, keys, target, rows):
    target_table, join, mismatch = _stage_batch(
        connection, schema, table, columns, keys, target, rows
    )
    # Omitted legacy columns receive their PostgreSQL defaults in staging.
    all_names = sql.SQL(", ").join(map(sql.Identifier, target))
    connection.execute(sql.SQL("INSERT INTO {} ({}) SELECT {} FROM ts_import_batch ON CONFLICT DO NOTHING")
                       .format(target_table, all_names, all_names))
    missing = sql.SQL("t.{} IS NULL").format(sql.Identifier(keys[0]))
    query = sql.SQL("SELECT EXISTS (SELECT 1 FROM ts_import_batch s LEFT JOIN {} t ON {} WHERE {} OR {})")
    if connection.execute(query.format(target_table, join, missing, mismatch)).fetchone()[0]:
        raise ImportConflict(f"{table} rows conflict with existing PostgreSQL data")


def inspect_backup(connection, backup: Path, *, schema: str = "trendscout", batch_size: int = 500,
                   require_present: bool = False):
    """Dry-run all rows against a provisioned schema without durable writes.

    Temporary tables validate PostgreSQL types and compare existing rows. This
    check cannot replace final reconciliation or a fence against later writes.
    """
    if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError("invalid PostgreSQL schema name")
    if not connection.autocommit:
        raise ValueError("inspection requires an autocommit connection")
    if type(batch_size) is not int or not 1 <= batch_size <= 5000:
        raise ValueError("batch size must be between 1 and 5000")
    verify_migrations(connection, schema=schema)
    path = Path(backup).absolute()
    initial_stat = path.stat()
    digest, size = _source_digest(path)
    after_digest = path.stat()
    if (initial_stat.st_size, initial_stat.st_mtime_ns) != (after_digest.st_size, after_digest.st_mtime_ns):
        raise InvalidBackup("source backup changed before inspection")
    counts = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
        _check_source(source)
        for table in TABLES:
            columns, keys, target = _table_metadata(connection, schema, source, table)
            counts[table] = source.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            if table == "collections":
                _collection_rows(source, columns)
                if source.execute("""SELECT 1 FROM collections WHERE category_id IS NOT NULL
                    GROUP BY category_id HAVING count(*) > 1 LIMIT 1""").fetchone():
                    raise InvalidBackup("source has duplicate collection categories")
            order = ",".join('"' + key + '"' for key in keys)
            cursor = source.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
            while rows := cursor.fetchmany(batch_size):
                with connection.transaction():
                    connection.execute("SELECT set_config('statement_timeout', '120s', true)")
                    target_table, join, _ = _stage_batch(
                        connection, schema, table, columns, keys, target, rows
                    )
                    if require_present:
                        missing = sql.SQL("t.{} IS NULL").format(sql.Identifier(keys[0]))
                        query = sql.SQL("SELECT EXISTS (SELECT 1 FROM ts_import_batch s "
                                        "LEFT JOIN {} t ON {} WHERE {})")
                        if connection.execute(query.format(target_table, join, missing)).fetchone()[0]:
                            raise ImportConflict(f"source {table} rows are missing from PostgreSQL")
                current_stat = path.stat()
                if ((current_stat.st_size, current_stat.st_mtime_ns)
                        != (initial_stat.st_size, initial_stat.st_mtime_ns)):
                    raise InvalidBackup("source backup changed during inspection")
    if _source_digest(path) != (digest, size):
        raise InvalidBackup("source backup changed during inspection")
    return {"source_sha256": digest, "size_bytes": size, "table_counts": counts}


def import_backup(connection, backup: Path, *, schema: str = "trendscout", batch_size: int = 500):
    """Import a standalone backup in resumable, conflict-detecting batches.

    The caller must use a direct autocommit connection and ensure no competing
    business writers. This routine never overwrites a different existing row.
    """
    if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError("invalid PostgreSQL schema name")
    if not connection.autocommit:
        raise ValueError("import requires an autocommit connection for durable batches")
    if type(batch_size) is not int or not 1 <= batch_size <= 5000:
        raise ValueError("batch size must be between 1 and 5000")
    verify_migrations(connection, schema=schema)
    path = Path(backup).absolute()
    initial_stat = path.stat()
    digest, size = _source_digest(path)
    after_digest = path.stat()
    if (initial_stat.st_size, initial_stat.st_mtime_ns) != (after_digest.st_size, after_digest.st_mtime_ns):
        raise InvalidBackup("source backup changed before import")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
        _check_source(source)
        for table in TABLES:
            columns, keys, target = _table_metadata(connection, schema, source, table)
            total = source.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            ordered_collections = _collection_rows(source, columns) if table == "collections" else None
            while True:
                with connection.transaction():
                    connection.execute("SELECT set_config('lock_timeout', '10s', true)")
                    connection.execute("SELECT set_config('statement_timeout', '120s', true)")
                    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                                       ("trendscout-import:" + schema,))
                    connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
                    connection.execute("INSERT INTO import_sources(sha256,size_bytes) VALUES (%s,%s) "
                                       "ON CONFLICT DO NOTHING", (digest, size))
                    connection.execute("INSERT INTO import_progress(source_sha256,table_name) VALUES (%s,%s) "
                                       "ON CONFLICT DO NOTHING", (digest, table))
                    offset, completed = connection.execute("""SELECT rows_processed, completed
                        FROM import_progress WHERE source_sha256=%s AND table_name=%s FOR UPDATE""",
                        (digest, table)).fetchone()
                    if offset > total or (completed and offset != total):
                        raise InvalidBackup("import journal does not match the source backup")
                    if completed:
                        break
                    if ordered_collections is not None:
                        rows = ordered_collections[offset:offset + batch_size]
                    else:
                        order = ",".join('"' + key + '"' for key in keys)
                        rows = source.execute(f'SELECT * FROM "{table}" ORDER BY {order} LIMIT ? OFFSET ?',
                                              (batch_size, offset)).fetchall()
                    if rows:
                        _copy_batch(connection, schema, table, columns, keys, target, rows)
                    next_offset = offset + len(rows)
                    if not rows and offset != total:
                        raise InvalidBackup("source row count changed during import")
                    current_stat = path.stat()
                    if ((current_stat.st_size, current_stat.st_mtime_ns)
                            != (initial_stat.st_size, initial_stat.st_mtime_ns)):
                        raise InvalidBackup("source backup changed during import")
                    connection.execute("""UPDATE import_progress SET rows_processed=%s, completed=%s,
                        updated_at=now() WHERE source_sha256=%s AND table_name=%s""",
                        (next_offset, next_offset == total, digest, table))
                    if next_offset == total:
                        break
    if _source_digest(path) != (digest, size):
        raise InvalidBackup("source backup changed during import")
    # A completed journal is not itself proof that rows remain present. Compare
    # every source row again, including on an idempotent rerun.
    return inspect_backup(connection, path, schema=schema, batch_size=batch_size,
                          require_present=True)
