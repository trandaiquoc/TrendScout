"""Migration ledger safety and optional isolated PostgreSQL integration."""

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import (
    Migration,
    MigrationMismatch,
    apply_migrations,
    bundled_migrations,
    verify_migrations,
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


def test_upgrade_from_version_twelve_verifies_0013_0014_checksum_and_compatibility():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    schema = "ts_test_db07_" + uuid.uuid4().hex
    migrations = bundled_migrations()
    assert len(migrations) == 14

    with postgres_connection({"DATABASE_URL_UNPOOLED": url}, direct=True) as connection:
        connection.autocommit = True
        assert connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0] is False
        created = False
        try:
            created = True
            assert apply_migrations(connection, schema=schema, migrations=migrations[:12]) == tuple(
                range(1, 13)
            )
            connection.execute(sql.SQL("""INSERT INTO {}(fetch_id,status,updated_at,stage)
                VALUES ('pre-ai08-fetch','COMPLETED',now(),'COMPLETED')""").format(
                    sql.Identifier(schema, "fetch_jobs")))
            connection.execute(sql.SQL("""INSERT INTO {}(id,name,description,support_ids,reason,
                status,created_at,updated_at) VALUES
                ('pre-ai08-proposal','Legacy proposal','Existing proposal',%s,'legacy','PENDING',now(),now())""")
                .format(sql.Identifier(schema, "proposals")), (Jsonb(["old-keyword-1", "old-keyword-2"]),))
            assert apply_migrations(connection, schema=schema) == (13, 14)
            verify_migrations(connection, schema=schema)
            assert apply_migrations(connection, schema=schema) == ()

            ledger = connection.execute(sql.SQL(
                "SELECT version,name,checksum FROM {}._schema_migrations ORDER BY version"
            ).format(sql.Identifier(schema))).fetchall()
            assert ledger == [(item.version, item.name, item.checksum) for item in migrations]
            fetch_row = connection.execute(sql.SQL(
                "SELECT status,ai_jobs FROM {} WHERE fetch_id='pre-ai08-fetch'"
            ).format(sql.Identifier(schema, "fetch_jobs"))).fetchone()
            assert fetch_row[0] == "COMPLETED"
            assert fetch_row[1] == []
            proposal_row = connection.execute(sql.SQL(
                "SELECT status,expires_at,accepted_collection_id FROM {} WHERE id='pre-ai08-proposal'"
            ).format(sql.Identifier(schema, "proposals"))).fetchone()
            assert tuple(proposal_row) == ("PENDING", None, None)
            columns = connection.execute(
                "SELECT table_name,column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND ("
                "(table_name='dataset_snapshots' AND column_name IN "
                "('source_ingestion_status','materialization_status','algorithm_processing_status',"
                "'ai_classification_status')) OR "
                "(table_name='classification_results' AND column_name='classification_input_digest') OR "
                "(table_name='fetch_jobs' AND column_name='ai_jobs') OR "
                "(table_name='proposals' AND column_name IN ('expires_at','accepted_collection_id'))) ",
                (schema,),
            ).fetchall()
            assert len(columns) == 8
            assert connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname=%s "
                "AND indexname='proposals_pending_expiry')", (schema,),
            ).fetchone()[0] is True
            assert connection.execute(
                "SELECT confdeltype FROM pg_constraint WHERE conrelid=to_regclass(%s) "
                "AND conname='proposals_accepted_collection_id_fkey'",
                (schema + '.proposals',),
            ).fetchone()[0] == "n"
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema),
                ))
