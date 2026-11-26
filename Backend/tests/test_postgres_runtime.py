"""Warm PostgreSQL runtimes reuse successful identity and schema checks only."""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.storage.postgres_runtime as postgres_runtime
from app.settings import Settings
from app.storage.postgres_runtime import (
    PostgresRuntimeNotReady,
    _validation_key,
    verify_postgres_runtime,
)


def _environment():
    suffix = uuid.uuid4().hex
    return {
        "DATABASE_URL": f"postgresql://user:secret@pool-{suffix}.example/db",
        "DATABASE_URL_UNPOOLED": f"postgresql://user:secret@direct-{suffix}.example/db",
    }


def test_runtime_validation_is_cached_per_database_and_schema():
    @contextmanager
    def connect(_environment, *, direct):
        assert direct is True
        yield object()

    environment = _environment()
    cache_key = _validation_key(environment, "runtime_test", require_cutover_data=False)
    assert environment["DATABASE_URL"] not in cache_key
    assert environment["DATABASE_URL_UNPOOLED"] not in cache_key
    assert cache_key != _validation_key(environment, "runtime_test")
    with (
        patch("app.storage.postgres_runtime.verify_same_database") as verify_identity,
        patch("app.storage.postgres_runtime.postgres_connection", connect),
        patch("app.storage.postgres_runtime.verify_migrations") as verify_schema,
    ):
        verify_postgres_runtime(
            environment, schema="runtime_test", require_cutover_data=False
        )
        verify_postgres_runtime(
            environment, schema="runtime_test", require_cutover_data=False
        )
        assert verify_identity.call_count == 1
        assert verify_schema.call_count == 1

        verify_postgres_runtime(
            environment, schema="another_schema", require_cutover_data=False
        )
        assert verify_identity.call_count == 2
        assert verify_schema.call_count == 2


def test_failed_runtime_validation_is_not_cached():
    @contextmanager
    def connect(_environment, *, direct):
        assert direct is True
        yield object()

    environment = _environment()
    with (
        patch(
            "app.storage.postgres_runtime.verify_same_database",
            side_effect=[RuntimeError("identity unavailable"), None],
        ) as verify_identity,
        patch("app.storage.postgres_runtime.postgres_connection", connect),
        patch("app.storage.postgres_runtime.verify_migrations") as verify_schema,
    ):
        with pytest.raises(RuntimeError, match="identity unavailable"):
            verify_postgres_runtime(
                environment, schema="runtime_failure_test", require_cutover_data=False
            )
        verify_postgres_runtime(
            environment, schema="runtime_failure_test", require_cutover_data=False
        )
        assert verify_identity.call_count == 2
        assert verify_schema.call_count == 1


def test_successful_runtime_validation_expires_after_five_minutes(monkeypatch):
    @contextmanager
    def connect(_environment, *, direct):
        assert direct is True
        yield object()

    environment = _environment()
    clock = [100.0]
    monkeypatch.setattr(
        postgres_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    with (
        patch("app.storage.postgres_runtime.verify_same_database") as verify_identity,
        patch("app.storage.postgres_runtime.postgres_connection", connect),
        patch("app.storage.postgres_runtime.verify_migrations") as verify_schema,
    ):
        verify_postgres_runtime(
            environment, schema="runtime_expiry_test", require_cutover_data=False
        )
        clock[0] = 399.0
        verify_postgres_runtime(
            environment, schema="runtime_expiry_test", require_cutover_data=False
        )
        assert verify_identity.call_count == 1
        clock[0] = 400.0
        verify_postgres_runtime(
            environment, schema="runtime_expiry_test", require_cutover_data=False
        )
        assert verify_identity.call_count == 2
        assert verify_schema.call_count == 2


def test_runtime_requires_nonempty_business_and_operational_cutover_state():
    population = (True, True, True, True)

    @contextmanager
    def connect(_environment, *, direct):
        assert direct is True
        yield SimpleNamespace(execute=lambda *_args: SimpleNamespace(
            fetchone=lambda: population
        ))

    environment = _environment()
    with (
        patch("app.storage.postgres_runtime.verify_same_database"),
        patch("app.storage.postgres_runtime.postgres_connection", connect),
        patch("app.storage.postgres_runtime.verify_migrations"),
    ):
        verify_postgres_runtime(environment, schema="runtime_ready_test")


def test_empty_runtime_state_fails_closed_and_is_not_cached():
    population = (False, False, False, False)

    @contextmanager
    def connect(_environment, *, direct):
        assert direct is True
        yield SimpleNamespace(execute=lambda *_args: SimpleNamespace(
            fetchone=lambda: population
        ))

    environment = _environment()
    with (
        patch("app.storage.postgres_runtime.verify_same_database") as verify_identity,
        patch("app.storage.postgres_runtime.postgres_connection", connect),
        patch("app.storage.postgres_runtime.verify_migrations") as verify_schema,
    ):
        for _ in range(2):
            with pytest.raises(PostgresRuntimeNotReady):
                verify_postgres_runtime(environment, schema="runtime_empty_test")
        assert verify_identity.call_count == 2
        assert verify_schema.call_count == 2


def test_vercel_app_rejects_unready_postgres_before_initializing_stores():
    from app.main import create_app

    settings = Settings.from_environment({
        "VERCEL": "1",
        "TREND_SCOUT_RUN_MODE": "live",
        "TREND_SCOUT_POSTGRES_RUNTIME_ENABLED": "true",
        "DATABASE_URL": "postgresql://fixture:fixture@localhost/trendscout",
        "DATABASE_URL_UNPOOLED": "postgresql://fixture:fixture@localhost/trendscout",
        "WIKIMEDIA_USER_AGENT": "TrendScoutTest/1.0 (https://example.invalid)",
    })
    postgres_environment = _environment()
    with (
        patch("app.storage.postgres_runtime.verify_postgres_runtime",
              side_effect=PostgresRuntimeNotReady("cutover data missing")) as verify,
        patch("app.storage.postgres_analysis.PostgresAnalysisStorage") as analysis_store,
    ):
        with pytest.raises(PostgresRuntimeNotReady, match="cutover data missing"):
            create_app(settings=settings, vercel_postgres_environment=postgres_environment)

    verify.assert_called_once_with(
        postgres_environment, schema="trendscout", require_cutover_data=True
    )
    analysis_store.assert_not_called()
