"""PostgreSQL connection foundation for opt-in runtimes and migrations.

Runtime leases come from a bounded process-local pool; direct connections are
reserved for explicit migration or administrative operations. No transaction
should span a provider request, and callers must not retry a write whose commit
outcome is unknown without an idempotency contract.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import re
import threading
from contextlib import ExitStack, contextmanager
from typing import Iterator, Mapping

import certifi
import psycopg
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool


class PostgresConfigurationError(ValueError):
    """A required PostgreSQL connection setting is missing."""


class PostgresConnectionError(RuntimeError):
    """A PostgreSQL connection could not be established."""


_POOL_MAX_SIZE = 4
_POOL_TIMEOUT_SECONDS = 10
_POOL_MAX_IDLE_SECONDS = 30
_SAFE_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_pool_lock = threading.Lock()
_connection_pools: dict[str, ConnectionPool] = {}


class _PoolLogRedactor(logging.Filter):
    """Keep pool warnings useful without forwarding driver diagnostics or DSNs."""

    def filter(self, record):
        if record.levelno >= logging.WARNING:
            details = []
            if isinstance(record.exc_info, tuple) and len(record.exc_info) > 1:
                error = record.exc_info[1]
                if isinstance(error, BaseException):
                    details = _safe_exception_details(error)
            record.msg = "PostgreSQL connection pool operation failed"
            if details:
                record.msg += f" ({'; '.join(details)})"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


_pool_logger = logging.getLogger("psycopg.pool")
_pool_logger.addFilter(_PoolLogRedactor())


def _reset_pooled_connection(connection: psycopg.Connection) -> None:
    """Restore mutable connection defaults before another runtime lease."""
    connection.row_factory = tuple_row


def _safe_exception_details(error: BaseException) -> list[str]:
    """Extract fixed-form metadata and allowlisted failure classes, never raw text."""
    error_type = type(error).__name__
    if not _SAFE_ERROR_TYPE.fullmatch(error_type):
        error_type = "PostgresError"
    details = [error_type]
    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(sqlstate, str) and _SAFE_SQLSTATE.fullmatch(sqlstate):
        details.append(f"SQLSTATE {sqlstate}")
    try:
        message = str(error).casefold()
    except Exception:
        message = ""
    categories = (
        ("authentication_failed", ("password authentication failed", "authentication failed",
                                    "no password supplied")),
        ("tls_validation_failed", ("certificate verify failed", "certificate validation failed",
                                   "ssl error", "tls error")),
        ("timeout", ("timed out", "timeout")),
        ("name_resolution_failed", ("could not translate host name", "name or service not known",
                                    "temporary failure in name resolution")),
        ("connection_refused", ("connection refused",)),
        ("network_unavailable", ("network is unreachable", "no route to host")),
        ("server_unavailable", ("server closed the connection unexpectedly",
                                 "connection reset by peer", "connection lost")),
    )
    for category, markers in categories:
        if any(marker in message for marker in markers):
            details.append(f"failure={category}")
            break
    return details


def _connection_error(name: str, error: Exception) -> PostgresConnectionError:
    """Build a useful connection diagnostic without consulting the driver message."""
    details = _safe_exception_details(error)
    return PostgresConnectionError(
        f"{name} connection failed ({'; '.join(details)})"
    )


def _pool_for(url: str) -> ConnectionPool:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    with _pool_lock:
        pool = _connection_pools.get(key)
        if pool is None or pool.closed:
            pool = ConnectionPool(
                conninfo=url,
                min_size=0,
                max_size=_POOL_MAX_SIZE,
                max_idle=_POOL_MAX_IDLE_SECONDS,
                timeout=_POOL_TIMEOUT_SECONDS,
                kwargs={
                    "sslmode": "verify-full",
                    "sslrootcert": certifi.where(),
                    "connect_timeout": _POOL_TIMEOUT_SECONDS,
                    "prepare_threshold": None,
                },
                name="trendscout-postgres",
                reset=_reset_pooled_connection,
                open=False,
            )
            pool.open(wait=False)
            _connection_pools[key] = pool
        return pool


def _close_connection_pools() -> None:
    with _pool_lock:
        pools = tuple(_connection_pools.values())
        _connection_pools.clear()
    for pool in pools:
        try:
            pool.close(timeout=1)
        except Exception:
            _pool_logger.warning("PostgreSQL connection pool cleanup failed")


def postgres_pool_stats() -> dict[str, int]:
    """Return process-local counters without exposing pool names or connection URLs."""
    with _pool_lock:
        pools = tuple(_connection_pools.values())
    totals = {
        "connections_num": 0,
        "connections_ms": 0,
        "requests_num": 0,
        "requests_wait_ms": 0,
    }
    for pool in pools:
        stats = pool.get_stats()
        for key in totals:
            totals[key] += int(stats.get(key, 0))
    return totals


atexit.register(_close_connection_pools)


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
    if direct:
        failure = None
        try:
            connection = psycopg.connect(
                url,
                sslmode="verify-full",
                sslrootcert=certifi.where(),
                connect_timeout=_POOL_TIMEOUT_SECONDS,
                prepare_threshold=None,
            )
        except Exception as error:
            failure = _connection_error(name, error)
        if failure is not None:
            raise failure from None
        try:
            yield connection
        finally:
            connection.close()
        return

    failure = None
    try:
        pool = _pool_for(url)
    except Exception as error:
        failure = _connection_error(name, error)
    if failure is not None:
        raise failure from None
    with ExitStack() as stack:
        failure = None
        try:
            connection = stack.enter_context(
                pool.connection(timeout=_POOL_TIMEOUT_SECONDS)
            )
        except Exception as error:
            # Driver diagnostics can contain the DSN; preserve only safe metadata.
            failure = _connection_error(name, error)
        if failure is not None:
            raise failure from None
        yield connection
