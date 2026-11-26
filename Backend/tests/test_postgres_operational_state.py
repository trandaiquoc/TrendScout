"""Operational admission initialization and quota-evidence tests."""

import copy
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from psycopg import sql

from app.jobs.public_admission import (
    AdmissionDenied,
    AdmissionPolicy,
    PoolLimit,
    PublicAdmission,
)
from app.sources.catalog import GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID
from app.storage.postgres_atomic_state import PostgresAtomicStateStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations

NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)


class MemoryAtomicStateStore:
    """Small serial transaction double for the evidence-state unit checks."""

    def __init__(self):
        self.state = {}
        self.lock = threading.Lock()

    def transact(self, _key, operation):
        with self.lock:
            updated, result = operation(copy.deepcopy(self.state))
            self.state = copy.deepcopy(updated)
            return copy.deepcopy(result)


def _policy():
    return AdmissionPolicy(
        enabled_sources=frozenset({GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID}),
        pools={
            "serpapi": PoolLimit(5, 20, 5, 20, 1),
            "wikimedia": PoolLimit(5, 20, 5, 20, 1),
        },
        plan_fingerprint="a" * 64,
        cooldown_seconds=30,
        enabled=True,
    )


def _admission(store):
    return PublicAdmission(
        store, _policy(), state_key="google-live", require_quota_evidence=True
    )


def _verify_google(admission, *, requests=0, cost=0):
    _verify_pool(admission, "serpapi", requests=requests, cost=cost)


def _verify_pool(admission, pool, *, requests=0, cost=0):
    admission.record_quota_window_evidence(
        pool, "daily", requests=requests, cost=cost,
        evidence_source="isolated_test_evidence", now=NOW,
    )
    admission.record_quota_window_evidence(
        pool, "monthly", requests=requests, cost=cost,
        evidence_source="isolated_test_evidence", now=NOW,
    )


def test_operational_initialization_is_idempotent_and_does_not_seed_zero_quota():
    store = MemoryAtomicStateStore()
    admission = _admission(store)

    first = admission.initialize_operational_state(now=NOW)
    second = admission.initialize_operational_state(now=NOW)

    assert first == second
    assert second["quota"] == {}
    assert second["quota_verification"]["serpapi"]["daily"]["status"] == "UNKNOWN"
    assert second["quota_verification"]["serpapi"]["monthly"]["status"] == "UNKNOWN"

    _verify_google(admission, requests=2, cost=2)
    before = copy.deepcopy(store.state)
    after = admission.initialize_operational_state(now=NOW)
    assert after["quota"] == before["quota"]
    assert after["quota_verification"] == before["quota_verification"]


def test_unknown_quota_blocks_admission_and_exact_evidence_allows_it():
    store = MemoryAtomicStateStore()
    admission = _admission(store)
    admission.initialize_operational_state(now=NOW)

    with pytest.raises(AdmissionDenied, match="QUOTA_UNKNOWN"):
        admission.admit(
            "db03-unknown-quota-request",
            now=NOW,
            request_metadata={"sources": [GOOGLE_SOURCE_ID]},
        )
    assert store.state["jobs"] == {}
    assert store.state["quota"] == {}

    _verify_google(admission)
    accepted = admission.admit(
        "db03-verified-quota-request",
        now=NOW,
        request_metadata={"sources": [GOOGLE_SOURCE_ID]},
    )
    claim = admission.claim(accepted["job_id"], "db03-worker", now=NOW)
    assert admission.reserve_attempt(
        accepted["job_id"], "db03-worker", claim["fence"],
        source_id=GOOGLE_SOURCE_ID, attempt_id="db03-provider-attempt-0001", now=NOW,
    ) == "NEW"
    assert admission.reserve_attempt(
        accepted["job_id"], "db03-worker", claim["fence"],
        source_id=GOOGLE_SOURCE_ID, attempt_id="db03-provider-attempt-0001", now=NOW,
    ) == "REPLAY"


def test_initialization_preserves_cooldown_and_fenced_active_job():
    store = MemoryAtomicStateStore()
    admission = _admission(store)
    admission.initialize_operational_state(now=NOW)
    _verify_google(admission)
    admission.record_quota_window_evidence(
        "wikimedia", "daily", requests=0, cost=0,
        evidence_source="isolated_test_evidence", now=NOW,
    )
    admission.record_quota_window_evidence(
        "wikimedia", "monthly", requests=0, cost=0,
        evidence_source="isolated_test_evidence", now=NOW,
    )
    job = admission.admit(
        "db03-live-job-request",
        now=NOW,
        request_metadata={"sources": [GOOGLE_SOURCE_ID]},
    )
    claim = admission.claim(job["job_id"], "db03-worker-one", now=NOW)
    starts_before = copy.deepcopy(store.state["starts"])

    admission.initialize_operational_state(now=NOW)

    assert store.state["starts"] == starts_before
    assert store.state["jobs"][job["job_id"]]["fence"] == claim["fence"]
    with pytest.raises(AdmissionDenied, match="LEASE_BUSY"):
        admission.claim(job["job_id"], "db03-worker-two", now=NOW)
    with pytest.raises(AdmissionDenied, match="COOLDOWN"):
        admission.admit(
            "db03-cooldown-request",
            now=NOW,
            request_metadata={"sources": [WIKIMEDIA_SOURCE_ID]},
        )


def test_concurrent_idempotent_admission_creates_one_job():
    store = MemoryAtomicStateStore()
    admissions = [_admission(store) for _ in range(6)]
    for admission in admissions[:1]:
        admission.initialize_operational_state(now=NOW)
        _verify_google(admission)

    def admit(admission):
        return admission.admit(
            "db03-concurrent-idempotency",
            now=NOW,
            request_metadata={"sources": [GOOGLE_SOURCE_ID]},
        )

    with ThreadPoolExecutor(max_workers=len(admissions)) as executor:
        results = list(executor.map(admit, admissions))

    assert len({result["job_id"] for result in results}) == 1
    assert sum(result["result"] == "CREATED" for result in results) == 1
    assert len(store.state["jobs"]) == 1


@contextmanager
def _isolated_postgres_admission():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        if connection.execute(
                "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)",
                (schema,),
        ).fetchone()[0]:
            raise AssertionError("isolated PostgreSQL test schema already exists")
        try:
            apply_migrations(connection, schema=schema)
            created = True
            store = PostgresAtomicStateStore(environment, schema=schema)
            yield store, _admission(store)
        finally:
            if created:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )


def test_operational_state_postgres_round_trip_and_concurrent_admission():
    with _isolated_postgres_admission() as (store, admission):
        first = admission.initialize_operational_state(now=NOW)
        admission.initialize_operational_state(now=NOW)
        assert first["quota"] == {}
        assert store.read_many(["google-live"])["google-live"]["quota"] == {}
        with pytest.raises(AdmissionDenied, match="QUOTA_UNKNOWN"):
            admission.admit(
                "db03-postgres-unknown-request",
                now=NOW,
                request_metadata={"sources": [GOOGLE_SOURCE_ID]},
            )

        _verify_google(admission)
        _verify_pool(admission, "wikimedia")
        admissions = [_admission(store) for _ in range(6)]
        with ThreadPoolExecutor(max_workers=len(admissions)) as executor:
            results = list(executor.map(
                lambda item: item.admit(
                    "db03-postgres-concurrent-request",
                    now=NOW,
                    request_metadata={"sources": [GOOGLE_SOURCE_ID]},
                ),
                admissions,
            ))
        assert len({result["job_id"] for result in results}) == 1
        assert sum(result["result"] == "CREATED" for result in results) == 1
        job_id = results[0]["job_id"]

        def claim(item):
            owner, coordinator = item
            try:
                return owner, coordinator.claim(job_id, owner, now=NOW)
            except AdmissionDenied as exc:
                return owner, exc.code

        owners = [(f"db03-worker-{index}", item)
                  for index, item in enumerate(admissions)]
        with ThreadPoolExecutor(max_workers=len(owners)) as executor:
            claims = list(executor.map(claim, owners))
        winners = [(owner, result) for owner, result in claims if isinstance(result, dict)]
        assert len(winners) == 1
        assert sum(result == "LEASE_BUSY" for _, result in claims) == len(claims) - 1

        admission.initialize_operational_state(now=NOW)
        with pytest.raises(AdmissionDenied, match="COOLDOWN"):
            admission.admit(
                "db03-postgres-cooldown-request",
                now=NOW,
                request_metadata={"sources": [WIKIMEDIA_SOURCE_ID]},
            )
        owner, claim_result = winners[0]
        assert admission.reserve_attempt(
            job_id, owner, claim_result["fence"], source_id=GOOGLE_SOURCE_ID,
            attempt_id="db03-postgres-provider-attempt", now=NOW,
        ) == "NEW"
        assert admission.reserve_attempt(
            job_id, owner, claim_result["fence"], source_id=GOOGLE_SOURCE_ID,
            attempt_id="db03-postgres-provider-attempt", now=NOW,
        ) == "REPLAY"
        before = store.read_many(["google-live"])["google-live"]
        admission.initialize_operational_state(now=NOW)
        after = store.read_many(["google-live"])["google-live"]
        assert after["quota"] == before["quota"]
        assert after["inflight"] == before["inflight"]
        assert after["jobs"][job_id]["fence"] == claim_result["fence"]
        state = store.read_many(["google-live"])["google-live"]
        assert len(state["jobs"]) == 1
        assert state["quota"]["serpapi:day:2026-11-26"] == {"requests": 1, "cost": 1}
        assert state["quota"]["serpapi:month:2026-11"] == {"requests": 1, "cost": 1}
