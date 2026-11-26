"""Safety checks for the pooled PostgreSQL connection foundation."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from psycopg.rows import dict_row, tuple_row

from app.storage.postgres_connection import (
    PostgresConfigurationError,
    PostgresConnectionError,
    _reset_pooled_connection,
    postgres_connection,
)


def test_connection_selects_direct_url_verifies_tls_and_closes():
    connection = MagicMock()
    with patch("app.storage.postgres_connection.psycopg.connect", return_value=connection) as connect:
        with postgres_connection({"DATABASE_URL_UNPOOLED": "direct-value"}, direct=True) as actual:
            assert actual is connection
        assert connect.call_args.args == ("direct-value",)
        assert connect.call_args.kwargs["sslmode"] == "verify-full"
        assert connect.call_args.kwargs["sslrootcert"]
        assert connect.call_args.kwargs["prepare_threshold"] is None
        connection.close.assert_called_once_with()


def test_connection_closes_on_caller_failure():
    connection = MagicMock()

    class Pool:
        @contextmanager
        def connection(self, *, timeout):
            self.timeout = timeout
            try:
                yield connection
            except Exception:
                self.returned_with_error = True
                raise
            else:
                self.returned_with_error = False

    pool = Pool()
    with patch("app.storage.postgres_connection._pool_for", return_value=pool):
        with pytest.raises(ValueError):
            with postgres_connection({"DATABASE_URL": "value"}):
                raise ValueError("caller failure")
    assert pool.timeout == 10
    assert pool.returned_with_error is True
    connection.close.assert_not_called()


def test_pool_reset_restores_tuple_rows_after_dict_row_consumer():
    connection = MagicMock()
    connection.row_factory = dict_row

    _reset_pooled_connection(connection)

    assert connection.row_factory is tuple_row


def test_missing_and_failed_connections_do_not_expose_credentials():
    with pytest.raises(PostgresConfigurationError):
        with postgres_connection({}):
            pass
    class FailingPool:
        @contextmanager
        def connection(self, *, timeout):
            raise psycopg.OperationalError("secret-in-driver-diagnostic")
            yield

    with patch("app.storage.postgres_connection._pool_for", return_value=FailingPool()):
        with pytest.raises(PostgresConnectionError) as captured:
            with postgres_connection({"DATABASE_URL": "secret-in-url"}):
                pass
    assert "secret" not in str(captured.value)
    assert captured.value.__suppress_context__
