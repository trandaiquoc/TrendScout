"""Safety checks for the pooled PostgreSQL connection foundation."""

import logging
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


def test_pytest_local_variable_output_is_disabled(pytestconfig):
    assert pytestconfig.option.showlocals is False


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
    assert "secret" not in repr(captured.value)
    assert captured.value.__context__ is None
    assert captured.value.__suppress_context__


@pytest.mark.parametrize("direct", [False, True], ids=["pooled", "direct"])
def test_connection_failures_keep_safe_metadata_without_driver_details(direct):
    marker = "postgresql://dummy-user:dummy-password@db.invalid/db?token=dummy-token"

    class DriverFailure(Exception):
        sqlstate = "08001"

    def fail(*_args, **_kwargs):
        raise DriverFailure(f"password authentication failed for dummy-user at {marker}")

    environment = ({"DATABASE_URL_UNPOOLED": marker} if direct
                   else {"DATABASE_URL": marker})
    target = "app.storage.postgres_connection.psycopg.connect" if direct else \
        "app.storage.postgres_connection._pool_for"
    with patch(target, side_effect=fail):
        with pytest.raises(PostgresConnectionError) as captured:
            with postgres_connection(environment, direct=direct):
                pass

    diagnostic = f"{captured.value} {captured.value!r}"
    assert "DriverFailure" in diagnostic
    assert "SQLSTATE 08001" in diagnostic
    assert "failure=authentication_failed" in diagnostic
    assert marker not in diagnostic
    assert "dummy-password" not in diagnostic
    assert "dummy-token" not in diagnostic
    assert captured.value.__context__ is None


def test_pool_log_redaction_removes_driver_message_and_exception(caplog):
    marker = "postgresql://dummy-user:dummy-password@db.invalid/db"
    logger = logging.getLogger("psycopg.pool")
    caplog.set_level(logging.WARNING, logger="psycopg.pool")
    try:
        raise psycopg.OperationalError(marker)
    except psycopg.Error:
        logger.warning("connection failed: %s", marker, exc_info=True)

    assert marker not in caplog.text
    assert "dummy-password" not in caplog.text
    assert "OperationalError" in caplog.text
    assert all(record.exc_info is None and record.exc_text is None
               for record in caplog.records)


def test_connection_failure_classification_does_not_echo_driver_text():
    marker = "postgresql://dummy-user:dummy-password@db.invalid/db"

    class DriverFailure(Exception):
        pass

    with patch("app.storage.postgres_connection.psycopg.connect",
               side_effect=DriverFailure(f"connection timed out for {marker}")):
        with pytest.raises(PostgresConnectionError) as captured:
            with postgres_connection({"DATABASE_URL_UNPOOLED": marker}, direct=True):
                pass

    assert "failure=timeout" in str(captured.value)
    assert marker not in str(captured.value)
    assert "dummy-password" not in repr(captured.value)
