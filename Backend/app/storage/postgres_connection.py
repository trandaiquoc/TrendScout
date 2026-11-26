"""PostgreSQL connection foundation for opt-in runtimes and migrations.

Connections are deliberately short-lived. No transaction should span a
provider request, and callers must not retry a write whose commit outcome is
unknown without an idempotency contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Mapping

import certifi
import psycopg


class PostgresConfigurationError(ValueError):
    """A required PostgreSQL connection setting is missing."""


class PostgresConnectionError(RuntimeError):
    """A PostgreSQL connection could not be established."""


@contextmanager
def postgres_connection(
    environment: Mapping[str, str], *, direct: bool = False
) -> Iterator[psycopg.Connection]:
    """Connect with verified TLS and close promptly, without logging the URL.

    Runtime callers use the pooled URL; migration callers may explicitly use
    the direct URL. This factory does not establish that they are the same
    database, run migrations, manage retries, or enable a runtime backend.
    """
    name = "DATABASE_URL_UNPOOLED" if direct else "DATABASE_URL"
    url = environment.get(name, "")
    if not isinstance(url, str) or not url.strip():
        raise PostgresConfigurationError(f"{name} is required")
    try:
        connection = psycopg.connect(
            url,
            sslmode="verify-full",
            sslrootcert=certifi.where(),
            connect_timeout=10,
            # Avoid session-specific prepared statements through a pooler.
            prepare_threshold=None,
        )
    except psycopg.Error:
        # Driver diagnostics can contain the DSN; do not expose them.
        raise PostgresConnectionError(f"{name} connection failed") from None
    try:
        yield connection
    finally:
        connection.close()
