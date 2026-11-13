import copy
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from app.jobs.public_admission import AdmissionDenied
from app.local_ledger import STATE_KEY
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.storage.d6_authorization_tranches import AuthorizationTranche, SameLedgerTranches
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore

NOW = "2026-11-13T00:00:00+00:00"
AUTH_ID = "future-test-auth"


def contract(status="INACTIVE", requests=2, starts=2, cost=2):
    return AuthorizationTranche(
        authorization_id=AUTH_ID, source_id=WIKIMEDIA_SOURCE_ID,
        approval_id="FAKE-APPROVAL-FOR-ISOLATED-TEST", approver="Fixture Approver",
        valid_from_utc="2026-11-12T00:00:00+00:00", expires_at_utc="2026-11-14T00:00:00+00:00",
        policy_version="fixture-v1", policy_digest="d" * 64,
        query_plan_fingerprint="e" * 64, endpoint_scope="top-per-country",
        country_scope="US", access_scope="fixture-only", observation_date="2026-11-12",
        maximum_requests=requests, maximum_attempts=requests, maximum_job_starts=starts,
        maximum_cost_units=cost, maximum_monetary_fee="0", currency="USD",
        processing_permission="DENIED", derived_metrics_permission="DENIED",
        retention_policy="NONE", raw_body_policy="DO_NOT_STORE", security_controls="offline-fixture",
        rollback_owner="Fixture Rollback Owner", authorization_status=status)


def make_legacy():
    return {"schema": 1, "policy_digest": "c" * 64, "operational_enabled": False,
            "jobs": {"old": {"state": "PARTIAL", "receipts": {"a" * 64: {
                "source_id": WIKIMEDIA_SOURCE_ID, "pool": "wikimedia", "reserved_at": NOW}}}},
            "keys": {}, "starts": [NOW], "quota": {
                "wikimedia:day:2026-11-13": {"requests": 1, "cost": 1},
                "wikimedia:month:2026-11": {"requests": 1, "cost": 1}},
            "inflight": {}, "local_smoke_source_counts": {WIKIMEDIA_SOURCE_ID: 1}}


def process_reserve_args(args):
    path, attempt, job = args
    store = LocalSQLiteAtomicStateStore(path, environment_profile="test")
    svc = SameLedgerTranches(store, designated_host_id="fixture-host", host_id="fixture-host")
    try:
        return svc.reserve_attempt(
            authorization_id=AUTH_ID, approval_id="FAKE-APPROVAL-FOR-ISOLATED-TEST",
            source_id=WIKIMEDIA_SOURCE_ID, policy_version="fixture-v1", policy_digest="d" * 64,
            query_plan_fingerprint="e" * 64,
            endpoint_scope="top-per-country", country_scope="US", access_scope="fixture-only",
            observation_date="2026-11-12", job_id=job, attempt_id=attempt,
            idempotency_key=f"idem-{attempt}", cost_units=1, monetary_fee="0",
            currency="USD", now_utc=NOW)
    except AdmissionDenied as exc:
        return str(exc)


class TrancheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.sqlite"
        self.store = LocalSQLiteAtomicStateStore(self.path, environment_profile="test")
        self.service = SameLedgerTranches(self.store, designated_host_id="fixture-host", host_id="fixture-host")
        self.store.transact(STATE_KEY, lambda _state: (make_legacy(), None))
        self.service.migrate_legacy(migrated_at_utc=NOW)

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        return self.store.transact(STATE_KEY, lambda value: (value, copy.deepcopy(value)))

    def activate_fake_for_test(self, *, requests=2, starts=2, cost=2):
        value = contract("ACTIVE", requests, starts, cost).validate()
        def operation(state):
            state["authorizations"].append(value)
            return state, None
        self.store.transact(STATE_KEY, operation)

    def reserve(self, attempt="attempt-1", job="job-1", idem=None, **changes):
        params = dict(authorization_id=AUTH_ID, approval_id="FAKE-APPROVAL-FOR-ISOLATED-TEST",
                      source_id=WIKIMEDIA_SOURCE_ID, policy_version="fixture-v1", policy_digest="d" * 64,
                      query_plan_fingerprint="e" * 64,
                      endpoint_scope="top-per-country", country_scope="US", access_scope="fixture-only",
                      observation_date="2026-11-12", job_id=job, attempt_id=attempt,
                      idempotency_key=idem or f"idem-{attempt}", cost_units=1,
                      monetary_fee="0", currency="USD", now_utc=NOW)
        params.update(changes)
        return self.service.reserve_attempt(**params)

    def test_empty_ledger_does_not_infer_approval_and_append_contract_stays_inactive(self):
        with self.assertRaises(AdmissionDenied):
            self.service.reserve_attempt(authorization_id=AUTH_ID, approval_id="x", source_id=WIKIMEDIA_SOURCE_ID,
                policy_version="fixture-v1", policy_digest="d"*64, query_plan_fingerprint="e"*64,
                endpoint_scope="x", country_scope="US", access_scope="x",
                observation_date="2026-11-12", job_id="job", attempt_id="a", idempotency_key="i",
                cost_units=1, monetary_fee="0", currency="USD", now_utc=NOW)
        self.service.append_inactive_authorization(contract())
        with self.assertRaises(AdmissionDenied):
            self.service.append_inactive_authorization(contract())
        with self.assertRaises(AdmissionDenied):
            self.reserve()
        self.assertEqual(self.state()["reservations"], [])

    def test_contract_rejects_missing_owner_values_old_approval_and_active_append(self):
        with self.assertRaises(ValueError):
            contract().__class__(**{**contract().__dict__, "approver": ""}).validate()
        with self.assertRaises(ValueError):
            contract().__class__(**{**contract().__dict__,
                                    "approval_id": "D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12"}).validate()
        with self.assertRaises(ValueError):
            contract().__class__(**{**contract("ACTIVE").__dict__, "approver": "PENDING"}).validate()
        self.assertEqual(contract("ACTIVE").validate()["authorization_status"], "ACTIVE")
        self.activate_fake_for_test()
        with self.assertRaises(AdmissionDenied):
            self.service.append_inactive_authorization(contract("ACTIVE"))

    def test_scope_approval_expiry_and_old_approval_fail_before_reservation(self):
        self.activate_fake_for_test()
        for overrides in (
            {"approval_id": "D6.2-WIKIMEDIA-ONE-REQUEST-2026-11-12"},
            {"country_scope": "CA"}, {"now_utc": "2026-11-15T00:00:00+00:00"},
            {"policy_digest": "f" * 64},
            {"currency": "EUR"},
            {"query_plan_fingerprint": "f" * 64},
        ):
            with self.assertRaises(AdmissionDenied):
                self.reserve(**overrides)
        self.assertEqual(self.state()["reservations"], [])

    def test_replay_crash_unknown_outcome_and_non_refundable_budget(self):
        self.activate_fake_for_test()
        self.assertEqual(self.reserve(), "NEW")
        snapshot = self.state()
        self.assertEqual(self.reserve(), "REPLAY")
        with self.assertRaises(AdmissionDenied):
            self.reserve(attempt="different", job="job-2")  # crashed reservation holds concurrency
        self.assertEqual(self.state()["reservations"], snapshot["reservations"])
        reservation = snapshot["reservations"][0]["reservation_id"]
        self.assertEqual(self.service.settle_attempt(reservation_id=reservation, outcome="HTTP_200_BODY_ERROR", now_utc=NOW), "SETTLED")
        self.assertEqual(self.service.settle_attempt(reservation_id=reservation, outcome="different", now_utc=NOW), "REPLAY")
        with self.assertRaises(AdmissionDenied):
            self.reserve(attempt="resumed", job="job-1")
        self.assertEqual(self.reserve(attempt="attempt-2", job="job-2"), "NEW")
        with self.assertRaises(AdmissionDenied):
            self.reserve(attempt="attempt-3", job="job-3")
        current = self.state()
        self.assertEqual(1 + len(current["reservations"]), 3)
        self.assertEqual(current["legacy_tranche"]["consumed_requests"], 1)
        self.assertNotIn("body", current["reservations"][0])

    def test_revoked_and_expired_authorizations_are_denied(self):
        revoked = contract("REVOKED").validate()
        self.store.transact(STATE_KEY, lambda state: ({**state, "authorizations": [revoked]}, None))
        with self.assertRaises(AdmissionDenied):
            self.reserve()
        expired = contract("ACTIVE").validate()
        expired["expires_at_utc"] = "2026-11-12T23:59:59+00:00"
        self.store.transact(STATE_KEY, lambda state: ({**state, "authorizations": [expired]}, None))
        with self.assertRaises(AdmissionDenied):
            self.reserve()

    def test_caps_cost_starts_and_duplicate_idempotency_are_atomic(self):
        self.activate_fake_for_test(requests=2, starts=1, cost=1)
        self.assertEqual(self.reserve(), "NEW")
        with self.assertRaises(AdmissionDenied):
            self.reserve(attempt="second", job="job-2")
        self.assertEqual(self.reserve(attempt="attempt-1", job="job-1"), "REPLAY")
        with self.assertRaises(AdmissionDenied):
            self.reserve(attempt="new", job="job-new", idem="idem-attempt-1")

    def test_two_processes_share_one_remaining_concurrency_slot(self):
        self.activate_fake_for_test()
        with ProcessPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(process_reserve_args, [
                (str(self.path), "proc-a", "job-a"), (str(self.path), "proc-b", "job-b")]))
        self.assertEqual(results.count("NEW"), 1, results)
        self.assertEqual(results.count("TRANCHE_CONCURRENCY_LIMIT"), 1, results)
        self.assertEqual(len(self.state()["reservations"]), 1)


if __name__ == "__main__":
    unittest.main()
