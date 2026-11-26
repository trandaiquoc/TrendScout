"""Staging old schedules without activating them or copying active leases."""

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

import pytest
from psycopg import sql

from app.scheduling.persistent import PersistentScheduleStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations
from app.storage.sqlite_scheduler_to_postgres import (
    import_scheduler_backup,
    inspect_scheduler_backup,
)
from app.storage.sqlite_to_postgres import InvalidBackup


def test_scheduler_backup_import_and_active_lease_rejection(tmp_path):
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    source_path = tmp_path / "source.sqlite"
    store = PersistentScheduleStore(source_path)
    now = datetime(2026, 11, 25, tzinfo=timezone.utc)
    schedule = store.create(name="Synthetic", frequency="DAILY", timezone_name="UTC", hour=9, now=now)
    store.set_enabled(schedule["id"], True, now=now)
    with closing(sqlite3.connect(source_path)) as source:
        source.execute("""INSERT INTO schedule_occurrences(id,schedule_id,scheduled_at,status,finished_at)
            VALUES ('finished',?,?,'COMPLETED',?)""", (schedule["id"], now.isoformat(), now.isoformat()))
        source.commit()
        backup = tmp_path / "backup.sqlite"
        with closing(sqlite3.connect(backup)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
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
            assert inspect_scheduler_backup(connection, backup, schema=schema)["table_counts"] == {
                "schedules": 1, "schedule_occurrences": 1}
            for _ in range(2):
                import_scheduler_backup(connection, backup, schema=schema, batch_size=1)
            row = connection.execute(sql.SQL("SELECT enabled,name FROM {}.schedules")
                                     .format(sql.Identifier(schema))).fetchone()
            assert row == (1, "Synthetic")
            row = connection.execute(sql.SQL("SELECT status,fence FROM {}.schedule_occurrences")
                                     .format(sql.Identifier(schema))).fetchone()
            assert row == ("COMPLETED", 0)
            with closing(sqlite3.connect(backup)) as source:
                source.execute("UPDATE schedule_occurrences SET status='RUNNING'")
                source.commit()
            with pytest.raises(InvalidBackup, match="running scheduler occurrences"):
                inspect_scheduler_backup(connection, backup, schema=schema)
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
