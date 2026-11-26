"""Explicit, transactional PostgreSQL schema migrations.

Migration execution is an operator action, never an application request side
effect. Each schema has its own checksum ledger and transaction-scoped lock.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files

from psycopg import sql

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statement: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.statement.encode("utf-8")).hexdigest()


class MigrationMismatch(RuntimeError):
    """The database ledger disagrees with the migration files."""


def bundled_migrations() -> tuple[Migration, ...]:
    directory = files("app.storage.postgres_schema")
    migrations = []
    for entry in directory.iterdir():
        match = re.fullmatch(r"(\d{4})_([a-z0-9_]+)\.sql", entry.name)
        if match:
            migrations.append(Migration(int(match[1]), match[2], entry.read_text(encoding="utf-8")))
    return _validate_migrations(migrations)


def _validate_migrations(migrations) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    if (not ordered or any(not isinstance(item, Migration) or not item.statement.strip()
                           or not re.fullmatch(r"[a-z0-9_]+", item.name)
                           for item in ordered)
            or [item.version for item in ordered] != list(range(1, len(ordered) + 1))
            or len({item.name for item in ordered}) != len(ordered)):
        raise ValueError("migrations must be nonempty, uniquely named and consecutively numbered")
    return ordered


def verify_migrations(connection, *, schema: str = "trendscout") -> None:
    """Require the exact bundled ledger without creating or changing schema."""
    if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError("invalid PostgreSQL schema name")
    if connection.execute("SELECT to_regclass(%s)", (schema + "._schema_migrations",)).fetchone()[0] is None:
        raise MigrationMismatch("database migration ledger is missing")
    rows = connection.execute(sql.SQL("SELECT version, name, checksum FROM {}._schema_migrations "
                                      "ORDER BY version").format(sql.Identifier(schema))).fetchall()
    expected = bundled_migrations()
    if (len(rows) != len(expected)
            or any(tuple(row) != (item.version, item.name, item.checksum)
                   for row, item in zip(rows, expected))):
        raise MigrationMismatch("database migration history does not match bundled migrations")


def apply_migrations(connection, *, schema: str = "trendscout", migrations=None) -> tuple[int, ...]:
    """Apply missing migrations atomically; reject missing or changed history.

    Caller supplies the connection and commits its enclosing transaction, if
    any. A failed migration rolls back this invocation. This function does not
    import data or change the application's runtime database selection.
    """
    if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError("invalid PostgreSQL schema name")
    ordered = _validate_migrations(migrations if migrations is not None else bundled_migrations())
    applied = []
    with connection.transaction():
        previous_settings = connection.execute("""SELECT current_setting('search_path'),
            current_setting('lock_timeout'), current_setting('statement_timeout'),
            current_setting('idle_in_transaction_session_timeout')""").fetchone()
        connection.execute("SELECT set_config('lock_timeout', '10s', true)")
        connection.execute("SELECT set_config('statement_timeout', '120s', true)")
        connection.execute("SELECT set_config('idle_in_transaction_session_timeout', '30s', true)")
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           ("trendscout-schema:" + schema,))
        connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        connection.execute("""CREATE TABLE IF NOT EXISTS _schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        rows = connection.execute(
            "SELECT version, name, checksum FROM _schema_migrations ORDER BY version"
        ).fetchall()
        if len(rows) > len(ordered):
            raise MigrationMismatch("database has unknown migrations")
        for row, expected in zip(rows, ordered):
            if tuple(row) != (expected.version, expected.name, expected.checksum):
                raise MigrationMismatch("database migration history does not match bundled migrations")
        for migration in ordered[len(rows):]:
            # Migration text is trusted code shipped with the application, not user input.
            connection.execute(migration.statement, prepare=False)
            connection.execute(
                "INSERT INTO _schema_migrations(version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
            applied.append(migration.version)
        for setting, previous in zip(
            ("search_path", "lock_timeout", "statement_timeout",
             "idle_in_transaction_session_timeout"), previous_settings
        ):
            connection.execute("SELECT set_config(%s, %s, true)", (setting, previous))
    return tuple(applied)
