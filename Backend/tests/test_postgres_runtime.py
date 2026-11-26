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
    postgres_live_state_block_reason,
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
    cache_key = _validation_key(environment, "runtime_test")
    assert environment["DATABASE_URL"] not in cache_key
    assert environment["DATABASE_URL_UNPOOLED"] not in cache_key
    with (
        patch("app.storage.postgres_runtime.verify_same_database") as verify_identity,
        patch("app.storage.postgres_runtime.postgres_connection", connect),
        patch("app.storage.postgres_runtime.verify_migrations") as verify_schema,
    ):
        verify_postgres_runtime(environment, schema="runtime_test")
        verify_postgres_runtime(environment, schema="runtime_test")
        assert verify_identity.call_count == 1
        assert verify_schema.call_count == 1

        verify_postgres_runtime(environment, schema="another_schema")
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
            verify_postgres_runtime(environment, schema="runtime_failure_test")
        verify_postgres_runtime(environment, schema="runtime_failure_test")
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
        verify_postgres_runtime(environment, schema="runtime_expiry_test")
        clock[0] = 399.0
        verify_postgres_runtime(environment, schema="runtime_expiry_test")
        assert verify_identity.call_count == 1
        clock[0] = 400.0
        verify_postgres_runtime(environment, schema="runtime_expiry_test")
        assert verify_identity.call_count == 2
        assert verify_schema.call_count == 2


def test_empty_schema_readiness_does_not_require_business_or_quota_rows():
    @contextmanager
    def connect(_environment, *, direct):
        assert direct is True
        yield object()

    environment = _environment()
    with (
        patch("app.storage.postgres_runtime.verify_same_database") as verify_identity,
        patch("app.storage.postgres_runtime.postgres_connection", connect),
        patch("app.storage.postgres_runtime.verify_migrations") as verify_schema,
    ):
        verify_postgres_runtime(environment, schema="runtime_fresh_start_test")
        verify_identity.assert_called_once_with(environment)
        verify_schema.assert_called_once()


def test_schema_checksum_failure_is_not_cached():
    from app.storage.postgres_migrations import MigrationMismatch

    @contextmanager
    def connect(_environment, *, direct):
        assert direct is True
        yield object()

    environment = _environment()
    with (
        patch("app.storage.postgres_runtime.verify_same_database"),
        patch("app.storage.postgres_runtime.postgres_connection", connect),
        patch("app.storage.postgres_runtime.verify_migrations",
              side_effect=[MigrationMismatch("checksum mismatch"), None]) as verify_schema,
    ):
        with pytest.raises(MigrationMismatch, match="checksum mismatch"):
            verify_postgres_runtime(environment, schema="runtime_checksum_test")
        verify_postgres_runtime(environment, schema="runtime_checksum_test")
        assert verify_schema.call_count == 2


def test_live_state_readiness_requires_verified_quota_and_cooldown_state():
    digest = "verified-policy"
    assert postgres_live_state_block_reason({}, policy_digest=digest) == (
        "POSTGRES_OPERATIONAL_STATE_UNVERIFIED"
    )
    state = {
        "schema": 1,
        "policy_digest": digest,
        "operational_enabled": True,
        "jobs": {},
        "keys": {},
        "starts": [],
        "quota": {"pool:day:2026-11-26": {"requests": 0, "cost": 0}},
        "inflight": {},
    }
    assert postgres_live_state_block_reason(state, policy_digest=digest) is None
    assert postgres_live_state_block_reason(state, policy_digest="other") == (
        "POSTGRES_OPERATIONAL_STATE_POLICY_MISMATCH"
    )
    state["starts"] = ["not-a-timestamp"]
    assert postgres_live_state_block_reason(state, policy_digest=digest) == (
        "POSTGRES_OPERATIONAL_STATE_INVALID"
    )


def test_vercel_app_rejects_invalid_postgres_schema_before_initializing_stores():
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
        patch("app.storage.postgres_runtime.initialize_postgres_runtime_defaults",
              side_effect=PostgresRuntimeNotReady("migration checksum mismatch")) as initialize,
        patch("app.storage.postgres_analysis.PostgresAnalysisStorage") as analysis_store,
    ):
        with pytest.raises(PostgresRuntimeNotReady, match="migration checksum mismatch"):
            create_app(settings=settings, vercel_postgres_environment=postgres_environment)

    initialize.assert_called_once_with(postgres_environment, schema="trendscout")
    analysis_store.assert_not_called()
