"""Offline validation and optional isolated PostgreSQL import exercise."""

import os
import sqlite3
import uuid
from contextlib import closing
from unittest.mock import patch

import pytest
from psycopg import sql

from app.collections.store import SCHEMA
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations
from app.storage.sqlite_to_postgres import (
    ImportConflict,
    InvalidBackup,
    _convert,
    import_backup,
    inspect_backup,
)


def test_invalid_json_and_naive_timestamp_fail_closed():
    with pytest.raises(InvalidBackup):
        _convert('{"x": 1, "x": 2}', "jsonb")
    with pytest.raises(InvalidBackup):
        _convert("2026-11-25T10:00:00", "timestamp with time zone")
    assert _convert("2026-11-25", "text") == "2026-11-25"


def test_import_resumes_and_rejects_conflicts_in_isolated_schema(tmp_path):
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    path = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(path)) as source:
        source.executescript(SCHEMA)
        timestamp = "2026-11-25T10:00:00+00:00"
        source.execute("""INSERT INTO collections(id,name,created_by,created_at,updated_at)
                          VALUES ('z-parent','Parent','USER',?,?)""", (timestamp, timestamp))
        source.execute("""INSERT INTO collections(id,name,parent_id,created_by,created_at,updated_at)
                          VALUES ('a-child','Child','z-parent','USER',?,?)""", (timestamp, timestamp))
        source.execute("""INSERT INTO keywords(id,keyword,normalized,source,observed_at,data_mode,evidence)
                          VALUES ('k','Example','example','source','2026-11-25','LIVE',
                          '{"views":0,"precise":0.1234567890123456789}')""")
        source.commit()
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection({"DATABASE_URL_UNPOOLED": url}, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            created = True
            assert inspect_backup(connection, path, schema=schema)["table_counts"]["collections"] == 2
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.collections")
                                      .format(sql.Identifier(schema))).fetchone()[0] == 0
            with pytest.raises(ImportConflict):
                inspect_backup(connection, path, schema=schema, require_present=True)
            from app.storage import sqlite_to_postgres

            copy_batch = sqlite_to_postgres._copy_batch
            calls = 0

            def interrupted(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated interruption")
                return copy_batch(*args, **kwargs)

            with patch.object(sqlite_to_postgres, "_copy_batch", side_effect=interrupted):
                with pytest.raises(RuntimeError, match="simulated interruption"):
                    import_backup(connection, path, schema=schema, batch_size=1)
            import_backup(connection, path, schema=schema, batch_size=1)
            import_backup(connection, path, schema=schema, batch_size=1)
            assert connection.execute(sql.SQL("SELECT count(*) FROM {}.collections")
                                      .format(sql.Identifier(schema))).fetchone()[0] == 2
            assert connection.execute(sql.SQL("SELECT observed_at, evidence->>'precise' FROM {}.keywords")
                                      .format(sql.Identifier(schema))).fetchone() == (
                                          "2026-11-25", "0.1234567890123456789"
                                      )
            with closing(sqlite3.connect(path)) as source:
                source.execute("UPDATE collections SET name='Different' WHERE id='z-parent'")
                source.commit()
            with pytest.raises(ImportConflict):
                inspect_backup(connection, path, schema=schema)
            with pytest.raises(ImportConflict):
                import_backup(connection, path, schema=schema, batch_size=1)
            assert connection.execute(sql.SQL("SELECT name FROM {}.collections WHERE id='z-parent'")
                                      .format(sql.Identifier(schema))).fetchone()[0] == "Parent"
        finally:
            if created:
                # Only the random schema created by this test is removed.
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
