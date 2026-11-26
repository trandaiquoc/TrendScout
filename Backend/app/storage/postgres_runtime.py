"""PostgreSQL schema readiness and idempotent Fresh Start metadata."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime

from psycopg import sql

from app.sources.catalog import SOURCE_CAPABILITIES
from app.storage.postgres_config import verify_same_database
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import verify_migrations

_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 32
_CACHE_LOCK = threading.Lock()
_VALIDATED: OrderedDict[str, float] = OrderedDict()


class PostgresRuntimeNotReady(RuntimeError):
    """The PostgreSQL runtime failed a required readiness check."""


def _validation_key(environment: Mapping[str, str], schema: str) -> str:
    if not isinstance(environment, Mapping) or not isinstance(schema, str):
        raise ValueError("valid PostgreSQL runtime configuration is required")
    values = (environment.get("DATABASE_URL"), environment.get("DATABASE_URL_UNPOOLED"), schema)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("both PostgreSQL URLs and a schema are required")
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def verify_postgres_runtime(environment: Mapping[str, str], *, schema: str) -> None:
    """Verify database identity and the exact bundled migration ledger.

    Fresh Start does not require historical business rows. This check is
    read-only: migrations remain an explicit operator action, and no schema is
    created or modified here. Failed checks are never cached.
    """
    key = _validation_key(environment, schema)
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

        _VALIDATED[key] = time.monotonic() + _CACHE_TTL_SECONDS
        while len(_VALIDATED) > _CACHE_MAX_ENTRIES:
            _VALIDATED.popitem(last=False)


def initialize_postgres_runtime_defaults(
    environment: Mapping[str, str], *, schema: str
) -> None:
    """Ensure static source metadata exists without changing operator config.

    This is safe to repeat. Existing source configuration, including retention
    policy, is left untouched. It deliberately does not create quota, cooldown,
    lease, Fetch, keyword, or dataset rows; LIVE Fetch readiness is checked
    separately and fails closed until its operational state is verified.
    """
    verify_postgres_runtime(environment, schema=schema)
    namespace = sql.Identifier(schema)
    statement = sql.SQL("""INSERT INTO {}.sources(id, display_name, configuration)
        VALUES (%s, %s, '{{}}'::jsonb) ON CONFLICT(id) DO NOTHING""").format(namespace)
    sources = [
        (item.source_id, item.display_name) for item in SOURCE_CAPABILITIES
    ]
    try:
        with postgres_connection(
            environment, direct="DATABASE_URL_UNPOOLED" in environment
        ) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.executemany(statement, sources)
    except Exception as error:
        # Driver diagnostics can contain connection details; surface only the
        # exception class to the startup caller.
        raise PostgresRuntimeNotReady(
            f"PostgreSQL source metadata initialization failed ({type(error).__name__})"
        ) from None


def postgres_live_state_block_reason(state, *, policy_digest: str) -> str | None:
    """Return why a PostgreSQL LIVE Fetch must remain disabled.

    This checks only durable admission shape and its policy identity. The
    admission transactions still enforce quotas, cooldowns, leases, and writer
    fencing for every actual operation.
    """
    if not isinstance(state, Mapping) or not state:
        return "POSTGRES_OPERATIONAL_STATE_UNVERIFIED"
    if state.get("schema") != 1:
        return "POSTGRES_OPERATIONAL_STATE_INVALID"
    if not isinstance(policy_digest, str) or state.get("policy_digest") != policy_digest:
        return "POSTGRES_OPERATIONAL_STATE_POLICY_MISMATCH"
    if state.get("operational_enabled") is not True:
        return "POSTGRES_OPERATIONAL_STATE_DISABLED"

    for name in ("jobs", "keys", "quota", "inflight"):
        if not isinstance(state.get(name), Mapping):
            return "POSTGRES_OPERATIONAL_STATE_INVALID"
    if not state["quota"]:
        return "POSTGRES_OPERATIONAL_STATE_UNVERIFIED"

    starts = state.get("starts")
    if not isinstance(starts, list):
        return "POSTGRES_OPERATIONAL_STATE_INVALID"
    for start in starts:
        if not isinstance(start, str):
            return "POSTGRES_OPERATIONAL_STATE_INVALID"
        try:
            parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            return "POSTGRES_OPERATIONAL_STATE_INVALID"
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return "POSTGRES_OPERATIONAL_STATE_INVALID"

    for entry in state["quota"].values():
        if (not isinstance(entry, Mapping)
                or type(entry.get("requests")) is not int or entry["requests"] < 0
                or type(entry.get("cost")) is not int or entry["cost"] < 0):
            return "POSTGRES_OPERATIONAL_STATE_INVALID"
    return None
