"""Warm PostgreSQL runtimes reuse successful identity and schema checks only."""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.storage.postgres_runtime as postgres_runtime
from app.storage.postgres_runtime import _validation_key, verify_postgres_runtime


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
