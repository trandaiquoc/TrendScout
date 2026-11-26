"""Coordination contract tests; real database use is isolated and optional."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg
import pytest
from psycopg import sql

from app.jobs.public_admission import AdmissionDenied, AdmissionPolicy, PoolLimit, PublicAdmission
from app.ports.atomic_state import CommitOutcomeUnknown
from app.sources.catalog import GOOGLE_SOURCE_ID
from app.storage.postgres_atomic_state import PostgresAtomicStateStore, _same
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations


def test_json_comparison_distinguishes_boolean_from_number():
    assert not _same({"count": False}, {"count": 0})
    assert _same({"a": 1, "b": [2]}, {"b": [2], "a": 1})


def test_ambiguous_commit_is_not_reported_success():
    class Connection:
        def execute(self, *_args):
            return None

        @contextmanager
        def transaction(self):
            yield
            raise psycopg.OperationalError("secret driver diagnostics")

    @contextmanager
    def fake_connection(_environment):
        yield Connection()

    store = PostgresAtomicStateStore({})
    with (patch("app.storage.postgres_atomic_state.postgres_connection", fake_connection),
          patch.object(store, "_load", return_value={"scope": {}}),
          patch.object(store, "_write")):
        with pytest.raises(CommitOutcomeUnknown) as captured:
            store.transact("scope", lambda _state: ({"reserved": 1}, "NEW"))
    assert "secret" not in str(captured.value)


def test_atomic_quota_and_incremental_checkpoints_in_isolated_schema():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            created = True
            store = PostgresAtomicStateStore(environment, schema=schema)
            original = {"jobs": {"one": {"source_checkpoints": {"a": {"n": 1}, "b": {"n": 2}}}},
                        "zero": False, "empty": {}}
            store.transact("state", lambda _old: (original, None))
            row = sql.SQL("SELECT xmin::text FROM {}.coordination_checkpoints "
                          "WHERE scope='state' AND job_id='one' AND source_id='b'").format(
                              sql.Identifier(schema))
            unchanged_xmin = connection.execute(row).fetchone()[0]

            def update(state):
                state["jobs"]["one"]["source_checkpoints"]["a"]["n"] = 3
                state["zero"] = 0
                return state, None

            store.transact("state", update)
            actual = store.read_many(["state", "missing"])
            assert actual["state"]["jobs"]["one"]["source_checkpoints"]["a"]["n"] == 3
            assert type(actual["state"]["zero"]) is int
            assert actual["state"]["empty"] == {} and actual["missing"] == {}
            assert connection.execute(row).fetchone()[0] == unchanged_xmin

            now = datetime(2026, 11, 25, tzinfo=timezone.utc)
            policy = AdmissionPolicy(enabled_sources=frozenset({GOOGLE_SOURCE_ID}),
                pools={"serpapi": PoolLimit(2, 5, 2, 5, 1, max_inflight=2)},
                plan_fingerprint="a" * 64, enabled=True)
            admission = PublicAdmission(store, policy, state_key="public")
            job = admission.admit("synthetic-idempotency-key", now=now)["job_id"]
            fence = admission.claim(job, "worker", now=now)["fence"]

            def reserve(index):
                try:
                    return admission.reserve_attempt(job, "worker", fence,
                        source_id=GOOGLE_SOURCE_ID, attempt_id=f"synthetic-attempt-{index:04d}", now=now)
                except AdmissionDenied as exc:
                    return exc.code

            with ThreadPoolExecutor(max_workers=4) as executor:
                outcomes = list(executor.map(reserve, range(4)))
            assert outcomes.count("NEW") == 2
            assert outcomes.count("QUOTA_EXHAUSTED") == 2
            assert admission.quota_snapshot("serpapi", now=now)["daily"]["used"] == 2

            claim_admission = PublicAdmission(store, policy, state_key="claim-test")
            other_job = claim_admission.admit("another-idempotency-key", now=now)["job_id"]

            def claim(index):
                owner = f"worker-{index}"
                try:
                    return owner, claim_admission.claim(other_job, owner, now=now)
                except AdmissionDenied as exc:
                    return owner, exc.code

            with ThreadPoolExecutor(max_workers=4) as executor:
                claims = list(executor.map(claim, range(4)))
            winners = [(owner, result) for owner, result in claims if isinstance(result, dict)]
            assert len(winners) == 1
            assert sum(result == "LEASE_BUSY" for _, result in claims) == 3
            owner, first = winners[0]
            later = now + timedelta(seconds=policy.lease_seconds + 1)
            second = claim_admission.claim(other_job, "replacement", now=later)
            assert second["fence"] > first["fence"]
            with pytest.raises(AdmissionDenied, match="LEASE_LOST"):
                claim_admission.reserve_attempt(other_job, owner, first["fence"],
                    source_id=GOOGLE_SOURCE_ID, attempt_id="stale-worker-attempt", now=later)
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
