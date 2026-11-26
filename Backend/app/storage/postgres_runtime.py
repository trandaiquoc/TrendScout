"""Short-lived, process-local validation cache for PostgreSQL runtimes."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping

from app.storage.postgres_config import verify_same_database
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import verify_migrations

_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 32
_CACHE_LOCK = threading.Lock()
_VALIDATED: OrderedDict[str, float] = OrderedDict()


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
    """Verify database identity and migration ledger once per warm runtime window.

    The cache key is a digest of both connection URLs and the schema; raw URLs
    are not retained in cache keys or logged. Failed checks are never cached.
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
