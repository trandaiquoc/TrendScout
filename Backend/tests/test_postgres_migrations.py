"""Migration ledger safety and optional isolated PostgreSQL integration."""

import os
import uuid

import psycopg
import pytest

from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import (
    Migration,
    MigrationMismatch,
    apply_migrations,
    bundled_migrations,
)


def test_bundled_versions_are_stable_and_consecutive():
    migrations = bundled_migrations()
    assert [item.version for item in migrations] == list(range(1, len(migrations) + 1))
    assert all(len(item.checksum) == 64 for item in migrations)


def test_rejects_unsafe_schema_before_using_connection():
    with pytest.raises(ValueError):
        apply_migrations(None, schema='public; DROP SCHEMA public')


def test_migrations_in_isolated_rolled_back_schema():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    schema = "ts_test_" + uuid.uuid4().hex

    class RollBackTest(Exception):
        pass

    with postgres_connection({"DATABASE_URL_UNPOOLED": url}, direct=True) as connection:
        with pytest.raises(RollBackTest):
            with connection.transaction():
                migrations = bundled_migrations()
                original_path = connection.execute("SHOW search_path").fetchone()[0]
                assert apply_migrations(connection, schema=schema) == tuple(
                    item.version for item in migrations
                )
                assert connection.execute("SHOW search_path").fetchone()[0] == original_path
                assert apply_migrations(connection, schema=schema) == ()
                changed = (Migration(1, migrations[0].name, migrations[0].statement + "\n-- changed"),)
                with pytest.raises(MigrationMismatch):
                    apply_migrations(connection, schema=schema, migrations=changed)
                broken = migrations + (Migration(len(migrations) + 1, "broken",
                                                   "CREATE TABLE failed_change (id int); SELECT 1/0;"),)
                with pytest.raises(psycopg.errors.DivisionByZero):
                    apply_migrations(connection, schema=schema, migrations=broken)
                assert connection.execute(
                    "SELECT to_regclass(%s)", (schema + ".failed_change",)
                ).fetchone()[0] is None
                assert connection.execute(
                    "SELECT to_regclass(%s)", (schema + ".keywords",)
                ).fetchone()[0] is not None
                raise RollBackTest
        # The entire schema was created inside the aborted outer transaction.
        assert connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname = %s)", (schema,)
        ).fetchone()[0] is False
