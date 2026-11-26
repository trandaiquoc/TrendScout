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
from app.normal_wikimedia_live_policy import normal_google_live_policy
from app.sources.catalog import (
    BING_SOURCE_ID,
    GOOGLE_SOURCE_ID,
    WIKIMEDIA_SOURCE_ID,
    YOUTUBE_SOURCE_ID,
)
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
    if pool == "serpapi":
        admission.set_serpapi_billing_cycle("2026-12-11", now=NOW)
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


def test_unknown_youtube_other_bucket_does_not_block_verified_wikimedia():
    store = MemoryAtomicStateStore()
    policy = normal_google_live_policy(bing_enabled=True, youtube_enabled=True)
    admission = PublicAdmission(
        store, policy, state_key="google-live", require_quota_evidence=True
    )
    admission.initialize_operational_state(now=NOW)
    _verify_pool(admission, "wikimedia")
    _verify_pool(admission, "youtube")

    result = admission.admit(
        "db03b-wikimedia-independent-request",
        now=NOW,
        request_metadata={"sources": [WIKIMEDIA_SOURCE_ID]},
    )
    assert result["result"] == "CREATED"
    assert admission.quota_unknown_pools(
        source_ids=[YOUTUBE_SOURCE_ID], now=NOW
    ) == ("youtube_other",)
    with pytest.raises(AdmissionDenied, match="QUOTA_UNKNOWN"):
        admission.admit(
            "db03b-youtube-other-unknown-request",
            now=NOW,
            request_metadata={"sources": [YOUTUBE_SOURCE_ID]},
        )


def test_serpapi_account_total_is_shared_and_unattributed_until_source_evidence():
    store = MemoryAtomicStateStore()
    policy = normal_google_live_policy(bing_enabled=True, youtube_enabled=True)
    admission = PublicAdmission(
        store, policy, state_key="google-live", require_quota_evidence=True
    )
    admission.initialize_operational_state(now=NOW)
    assert admission.serpapi_account_evidence_needed(now=NOW)

    admission.set_serpapi_billing_cycle("2026-12-11", now=NOW)
    admission.reconcile_serpapi_account_usage("2026-12-11", 42, now=NOW)

    state = store.state
    assert state["quota"]["serpapi:cycle:2026-12-11"] == {
        "requests": 42, "cost": 42,
    }
    assert state["serpapi_account_usage_unattributed"] == {
        "cycle_ends_at": "2026-12-11", "requests": 42, "cost": 42,
    }
    assert state["quota_verification"]["serpapi"]["monthly"]["status"] == "VERIFIED"
    assert state["quota_verification"]["serpapi_google"]["monthly"]["status"] == "UNKNOWN"
    assert state["quota_verification"]["serpapi_bing"]["monthly"]["status"] == "UNKNOWN"
    assert not admission.serpapi_account_evidence_needed(now=NOW)


def test_postgres_cutover_rollover_creates_only_new_window_evidence_and_preserves_cooldown():
    store = MemoryAtomicStateStore()
    admission = PublicAdmission(
        store, _policy(), state_key="google-live", require_quota_evidence=True
    )
    before_boundary = datetime(2026, 11, 26, 23, 59, 45, tzinfo=timezone.utc)
    after_boundary = datetime(2026, 11, 27, 0, 0, 5, tzinfo=timezone.utc)
    admission.initialize_operational_state(now=before_boundary)
    for period, requests in (("daily", 3), ("monthly", 5)):
        admission.record_quota_window_evidence(
            "wikimedia", period, requests=requests, cost=requests,
            evidence_source="isolated_test_evidence", now=before_boundary,
        )

    def seed_recent_cooldown(state):
        state["starts"] = [before_boundary.isoformat()]
        return state, None

    store.transact("google-live", seed_recent_cooldown)
    admission.initialize_operational_state(now=after_boundary)

    state = store.state
    assert state["quota"]["wikimedia:day:2026-11-26"] == {"requests": 3, "cost": 3}
    assert state["quota"]["wikimedia:day:2026-11-27"] == {"requests": 0, "cost": 0}
    assert state["quota_verification"]["wikimedia"]["daily"] == {
        "window": "wikimedia:day:2026-11-27",
        "status": "VERIFIED",
        "evidence_source": "postgres_period_rollover",
        "verified_at": after_boundary.isoformat(),
    }
    assert state["quota"]["wikimedia:month:2026-11"] == {"requests": 5, "cost": 5}
    assert state["starts"] == [before_boundary.isoformat()]
    with pytest.raises(AdmissionDenied, match="COOLDOWN"):
        admission.admit(
            "db03b-cooldown-survives-midnight",
            now=after_boundary,
            request_metadata={"sources": [WIKIMEDIA_SOURCE_ID]},
        )


def test_rollover_never_zeroes_an_existing_unverified_counter():
    store = MemoryAtomicStateStore()
    admission = PublicAdmission(
        store, normal_google_live_policy(), state_key="google-live",
        require_quota_evidence=True,
    )
    admission.initialize_operational_state(now=NOW)

    def seed_unverified_usage(state):
        state["quota"]["wikimedia:day:2026-11-27"] = {
            "requests": 7, "cost": 7,
        }
        return state, None

    store.transact("google-live", seed_unverified_usage)
    next_day = datetime(2026, 11, 27, 9, tzinfo=timezone.utc)
    admission.initialize_operational_state(now=next_day)

    state = store.state
    assert state["quota"]["wikimedia:day:2026-11-27"] == {
        "requests": 7, "cost": 7,
    }
    assert state["quota_verification"]["wikimedia"]["daily"] == {
        "window": "wikimedia:day:2026-11-27", "status": "UNKNOWN",
    }


def test_rollover_does_not_mask_invalid_quota_status():
    store = MemoryAtomicStateStore()
    admission = PublicAdmission(
        store, _policy(), state_key="google-live", require_quota_evidence=True
    )
    admission.initialize_operational_state(now=NOW)

    def seed_invalid_status(state):
        state["quota_verification"]["wikimedia"]["daily"]["status"] = "UNVERIFIED"
        return state, None

    store.transact("google-live", seed_invalid_status)
    with pytest.raises(AdmissionDenied, match="STATE_INVALID"):
        admission.initialize_operational_state(
            now=datetime(2026, 11, 27, 9, tzinfo=timezone.utc)
        )


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
def _isolated_postgres_admission(admission_factory=_admission):
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
            yield store, admission_factory(store)
        finally:
            if created:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )


def _postgres_all_source_admission(store):
    policy = normal_google_live_policy(bing_enabled=True, youtube_enabled=True)
    return PublicAdmission(
        store, policy, state_key="google-live", require_quota_evidence=True
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
        assert state["quota"]["serpapi:cycle:2026-12-11"] == {
            "requests": 1, "cost": 1,
        }


def test_postgres_serpapi_account_total_stays_unattributed_across_google_and_bing():
    with _isolated_postgres_admission(_postgres_all_source_admission) as (store, admission):
        admission.initialize_operational_state(now=NOW)
        admission.set_serpapi_billing_cycle("2026-12-11", now=NOW)
        admission.reconcile_serpapi_account_usage("2026-12-11", 42, now=NOW)

        state = store.read_many(["google-live"])["google-live"]
        assert state["quota"]["serpapi:cycle:2026-12-11"] == {
            "requests": 42, "cost": 42,
        }
        assert state["serpapi_account_usage_unattributed"] == {
            "cycle_ends_at": "2026-12-11", "requests": 42, "cost": 42,
        }
        assert state["quota_verification"]["serpapi"]["monthly"]["status"] == "VERIFIED"
        assert state["quota_verification"]["serpapi_google"]["monthly"]["status"] == "UNKNOWN"
        assert state["quota_verification"]["serpapi_bing"]["monthly"]["status"] == "UNKNOWN"
        assert "serpapi" in admission.quota_unknown_pools(
            source_ids=[GOOGLE_SOURCE_ID], now=NOW
        )
        assert "serpapi" in admission.quota_unknown_pools(
            source_ids=[BING_SOURCE_ID], now=NOW
        )

        admission.record_quota_window_evidence(
            "wikimedia", "daily", requests=0, cost=0,
            evidence_source="isolated_test_evidence", now=NOW,
        )
        admission.record_quota_window_evidence(
            "wikimedia", "monthly", requests=0, cost=0,
            evidence_source="isolated_test_evidence", now=NOW,
        )
        assert admission.admit(
            "db03b-postgres-wikimedia-independent",
            now=NOW,
            request_metadata={"sources": [WIKIMEDIA_SOURCE_ID]},
        )["result"] == "CREATED"


def test_postgres_internal_quota_windows_roll_over_after_cutover():
    with _isolated_postgres_admission() as (store, admission):
        admission.initialize_operational_state(now=NOW)
        after_month_boundary = datetime(2026, 12, 1, 9, tzinfo=timezone.utc)
        admission.initialize_operational_state(now=after_month_boundary)

        state = store.read_many(["google-live"])["google-live"]
        wiki = state["quota_verification"]["wikimedia"]
        assert wiki["daily"]["window"] == "wikimedia:day:2026-12-01"
        assert wiki["daily"]["status"] == "VERIFIED"
        assert wiki["monthly"]["window"] == "wikimedia:month:2026-12"
        assert wiki["monthly"]["status"] == "VERIFIED"
        assert state["quota"]["wikimedia:day:2026-12-01"] == {
            "requests": 0, "cost": 0,
        }
        assert state["quota"]["wikimedia:month:2026-12"] == {
            "requests": 0, "cost": 0,
        }
        # Provider billing cycles are never inferred from the calendar.
        assert state["quota_verification"]["serpapi"]["monthly"]["window"] is None
        assert state["quota_verification"]["serpapi"]["monthly"]["status"] == "UNKNOWN"
        assert admission.admit(
            "db03b-postgres-wikimedia-after-rollover",
            now=after_month_boundary,
            request_metadata={"sources": [WIKIMEDIA_SOURCE_ID]},
        )["result"] == "CREATED"


def test_postgres_youtube_requires_both_quota_buckets_then_rolls_over_at_pacific_midnight():
    with _isolated_postgres_admission(_postgres_all_source_admission) as (store, admission):
        console_observed_at = datetime(2026, 11, 26, 8, 5, tzinfo=timezone.utc)
        admission.initialize_operational_state(now=console_observed_at)
        for pool in ("youtube", "youtube_other"):
            admission.record_quota_window_evidence(
                pool, "daily", requests=0, cost=0,
                evidence_source="google_cloud_console_trendscout_demo_youtube",
                now=console_observed_at,
            )

        assert admission.quota_unknown_pools(
            source_ids=[YOUTUBE_SOURCE_ID], now=console_observed_at
        ) == ("youtube", "youtube_other")
        with pytest.raises(AdmissionDenied, match="QUOTA_UNKNOWN"):
            admission.admit(
                "db03b-youtube-month-unknown",
                now=console_observed_at,
                request_metadata={"sources": [YOUTUBE_SOURCE_ID]},
            )

        after_daily_and_monthly_rollover = datetime(2026, 12, 1, 9, tzinfo=timezone.utc)
        admission.initialize_operational_state(now=after_daily_and_monthly_rollover)
        state = store.read_many(["google-live"])["google-live"]
        for pool in ("youtube", "youtube_other"):
            assert state["quota_verification"][pool]["daily"]["status"] == "VERIFIED"
            assert state["quota_verification"][pool]["monthly"]["status"] == "VERIFIED"
            assert state["quota"][f"{pool}:day:2026-12-01"] == {
                "requests": 0, "cost": 0,
            }
            assert state["quota"][f"{pool}:month:2026-12"] == {
                "requests": 0, "cost": 0,
            }
        assert admission.quota_unknown_pools(
            source_ids=[YOUTUBE_SOURCE_ID], now=after_daily_and_monthly_rollover
        ) == ()
        assert admission.admit(
            "db03b-youtube-after-rollover",
            now=after_daily_and_monthly_rollover,
            request_metadata={"sources": [YOUTUBE_SOURCE_ID]},
        )["result"] == "CREATED"
