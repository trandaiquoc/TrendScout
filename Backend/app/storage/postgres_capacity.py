"""Read-only PostgreSQL relation and connection capacity snapshot."""

from __future__ import annotations

from psycopg import sql

from app.storage.postgres_migrations import _SCHEMA_NAME, verify_migrations


def inspect_capacity(connection, *, schema: str = "trendscout", top_n: int = 10) -> dict:
    """Measure PostgreSQL database/schema usage without inferring Neon quota.

    Database and relation sizes are PostgreSQL measurements. They do not
    include Neon project-wide storage accounting, other branches, provider
    compute usage, or billing headroom.
    """
    if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError("invalid PostgreSQL schema name")
    if type(top_n) is not int or not 1 <= top_n <= 100:
        raise ValueError("top_n must be between 1 and 100")

    verify_migrations(connection, schema=schema)
    schema_summary = connection.execute(
        """SELECT count(*), COALESCE(sum(pg_relation_size(c.oid)), 0),
                  COALESCE(sum(pg_indexes_size(c.oid)), 0),
                  COALESCE(sum(pg_total_relation_size(c.oid)), 0)
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'm')""",
        (schema,),
    ).fetchone()
    relation_rows = connection.execute(
        """SELECT c.relname, GREATEST(c.reltuples, 0)::bigint,
                  pg_relation_size(c.oid), pg_indexes_size(c.oid),
                  pg_total_relation_size(c.oid)
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'm')
           ORDER BY pg_total_relation_size(c.oid) DESC, c.relname
           LIMIT %s""",
        (schema, top_n),
    ).fetchall()
    versions = connection.execute(
        sql.SQL("SELECT max(version) FROM {}._schema_migrations").format(sql.Identifier(schema))
    ).fetchone()[0]
    connection_count = connection.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
    ).fetchone()[0]
    max_connections = int(connection.execute("SELECT current_setting('max_connections')").fetchone()[0])

    return {
        "database": {
            "name": connection.info.dbname,
            "server_version_num": int(connection.info.server_version),
            "size_bytes": connection.execute(
                "SELECT pg_database_size(current_database())"
            ).fetchone()[0],
            "connections": connection_count,
            "max_connections": max_connections,
        },
        "schema": {
            "name": schema,
            "latest_migration_version": versions,
            "relation_count": schema_summary[0],
            "heap_bytes": int(schema_summary[1]),
            "index_bytes": int(schema_summary[2]),
            "total_relation_bytes": int(schema_summary[3]),
            "largest_relations": [
                {
                    "name": row[0],
                    "estimated_rows": row[1],
                    "heap_bytes": row[2],
                    "index_bytes": row[3],
                    "total_bytes": row[4],
                }
                for row in relation_rows
            ],
        },
        "neon_project_capacity": {
            "usage_verified": False,
            "headroom_bytes": None,
            "reason": "PostgreSQL relation sizes do not report Neon project-wide usage or quota.",
        },
    }
