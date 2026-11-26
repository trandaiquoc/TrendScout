"""Short-lived, process-local validation cache for PostgreSQL runtimes."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping

from psycopg import sql

from app.storage.postgres_config import verify_same_database
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import verify_migrations

_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 32
_CACHE_LOCK = threading.Lock()
_VALIDATED: OrderedDict[str, float] = OrderedDict()


class PostgresRuntimeNotReady(RuntimeError):
    """The database is provisioned but does not contain migrated runtime state."""


def _validation_key(
    environment: Mapping[str, str], schema: str, *, require_cutover_data: bool = True
) -> str:
    if (not isinstance(environment, Mapping) or not isinstance(schema, str)
            or type(require_cutover_data) is not bool):
        raise ValueError("valid PostgreSQL runtime configuration is required")
    values = (environment.get("DATABASE_URL"), environment.get("DATABASE_URL_UNPOOLED"), schema)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("both PostgreSQL URLs and a schema are required")
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    digest.update(b"cutover-ready" if require_cutover_data else b"schema-only")
    return digest.hexdigest()


def verify_postgres_runtime(
    environment: Mapping[str, str], *, schema: str, require_cutover_data: bool = True
) -> None:
    """Verify identity, migrations, and non-empty cutover data in a warm runtime.

    The cache key is a digest of both connection URLs and the schema; raw URLs
    are not retained in cache keys or logged. By default, an activated runtime
    also requires business rows and previously imported quota state, so a
    schema-only deployment cannot silently start with empty stores. Failed
    checks are never cached.
    """
    key = _validation_key(
        environment, schema, require_cutover_data=require_cutover_data
    )
    now = time.monotonic()
    with _CACHE_LOCK:
        expires_at = _VALIDATED.get(key)
        if expires_at is not None and expires_at > now:
            _VALIDATED.move_to_end(key)
            return
        _VALIDATED.pop(key, None)

        verify_same_database(environment)
        with postgres_connection(
            environment, direct="DATABASE_URL_UNPOOLED" in environment
        ) as connection:
            verify_migrations(connection, schema=schema)
            if require_cutover_data:
                namespace = sql.Identifier(schema)
                state_scope = "normal-google-live-v1"
                query = sql.SQL("""SELECT
                    EXISTS (SELECT 1 FROM {}.keywords),
                    EXISTS (SELECT 1 FROM {}.dataset_snapshots),
                    EXISTS (SELECT 1 FROM {}.coordination_fields
                            WHERE scope=%s AND field='policy_digest' AND NOT is_mapping
                              AND value IS NOT NULL),
                    EXISTS (SELECT 1 FROM {}.coordination_fields f
                            WHERE f.scope=%s AND f.field='quota' AND f.is_mapping
                              AND EXISTS (SELECT 1 FROM {}.coordination_entries e
                                          WHERE e.scope=f.scope AND e.field=f.field))""").format(
                    namespace, namespace, namespace, namespace, namespace,
                )
                populated = connection.execute(query, (state_scope, state_scope)).fetchone()
                if populated is None or len(populated) != 4 or not all(populated):
                    raise PostgresRuntimeNotReady(
                        "PostgreSQL runtime requires migrated keywords, dataset and quota state"
                    )

        _VALIDATED[key] = time.monotonic() + _CACHE_TTL_SECONDS
        while len(_VALIDATED) > _CACHE_MAX_ENTRIES:
            _VALIDATED.popitem(last=False)
