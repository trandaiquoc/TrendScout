"""Safety checks for the standalone PostgreSQL connection foundation."""

from unittest.mock import MagicMock, patch

import psycopg
import pytest

from app.storage.postgres_connection import (
    PostgresConfigurationError,
    PostgresConnectionError,
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
    with patch("app.storage.postgres_connection.psycopg.connect", return_value=connection):
        with pytest.raises(ValueError):
            with postgres_connection({"DATABASE_URL": "value"}):
                raise ValueError("caller failure")
    connection.close.assert_called_once_with()


def test_missing_and_failed_connections_do_not_expose_credentials():
    with pytest.raises(PostgresConfigurationError):
        with postgres_connection({}):
            pass
    with patch("app.storage.postgres_connection.psycopg.connect",
               side_effect=psycopg.OperationalError("secret-in-driver-diagnostic")):
        with pytest.raises(PostgresConnectionError) as captured:
            with postgres_connection({"DATABASE_URL": "secret-in-url"}):
                pass
    assert "secret" not in str(captured.value)
    assert captured.value.__suppress_context__
