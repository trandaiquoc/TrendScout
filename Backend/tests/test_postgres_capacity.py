import json
import os
import uuid

import pytest
from psycopg import sql

from app.storage.postgres_capacity import inspect_capacity
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations, bundled_migrations


def test_capacity_snapshot_measures_schema_without_claiming_project_headroom():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection({"DATABASE_URL_UNPOOLED": url}, direct=True) as connection:
        connection.autocommit = True
        try:
            apply_migrations(connection, schema=schema)
            created = True
            result = inspect_capacity(connection, schema=schema, top_n=5)
            json.dumps(result)

            assert result["database"]["size_bytes"] > 0
            assert result["database"]["connections"] > 0
            assert result["database"]["max_connections"] > 0
            assert result["schema"]["relation_count"] >= 30
            assert result["schema"]["latest_migration_version"] == len(bundled_migrations())
            assert all(item["total_bytes"] >= item["heap_bytes"]
                       for item in result["schema"]["largest_relations"])
            assert result["neon_project_capacity"] == {
                "usage_verified": False,
                "headroom_bytes": None,
                "reason": "PostgreSQL relation sizes do not report Neon project-wide usage or quota.",
            }
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_capacity_rejects_invalid_schema_and_limits_without_connecting():
    class UnusedConnection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("invalid input must fail before issuing SQL")

    with pytest.raises(ValueError, match="invalid PostgreSQL schema"):
        inspect_capacity(UnusedConnection(), schema="invalid-name")
    with pytest.raises(ValueError, match="top_n"):
        inspect_capacity(UnusedConnection(), top_n=101)
