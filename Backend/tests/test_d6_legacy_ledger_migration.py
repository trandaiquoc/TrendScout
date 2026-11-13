import copy
import tempfile
import unittest
from pathlib import Path

from app.jobs.public_admission import AdmissionDenied
from app.local_ledger import STATE_KEY
from app.sources.catalog import BING_SOURCE_ID, WIKIMEDIA_SOURCE_ID
from app.storage.d6_authorization_tranches import SameLedgerTranches
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore

NOW = "2026-11-13T00:00:00+00:00"


def legacy_state(*, partial=True, extra_source=False):
    wiki_receipt = {"source_id": WIKIMEDIA_SOURCE_ID, "pool": "wikimedia",
                    "reserved_at": NOW}
    job = {"state": "PARTIAL" if partial else "FAILED", "receipts": {"a" * 64: wiki_receipt}}
    counts = {WIKIMEDIA_SOURCE_ID: 1}
    quota = {"wikimedia:day:2026-11-13": {"requests": 1, "cost": 1},
             "wikimedia:month:2026-11": {"requests": 1, "cost": 1}}
    if extra_source:
        job["receipts"]["b" * 64] = {"source_id": BING_SOURCE_ID, "pool": "serpapi",
                                     "reserved_at": NOW}
        counts[BING_SOURCE_ID] = 1
        quota.update({"serpapi:day:2026-11-13": {"requests": 1, "cost": 1},
                      "serpapi:month:2026-11": {"requests": 1, "cost": 1}})
    return {"schema": 1, "policy_digest": "c" * 64, "operational_enabled": True,
            "jobs": {"historical-job": job}, "keys": {}, "starts": [NOW],
            "quota": quota, "inflight": {}, "local_smoke_source_counts": counts,
            "last_observed_at": NOW}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite"
        self.store = LocalSQLiteAtomicStateStore(self.path, environment_profile="test")
        self.service = SameLedgerTranches(self.store, designated_host_id="test-host", host_id="test-host")

    def tearDown(self):
        self.temp.cleanup()

    def put(self, value):
        self.store.transact(STATE_KEY, lambda _old: (copy.deepcopy(value), None))

    def get(self):
        return self.store.transact(STATE_KEY, lambda state: (state, copy.deepcopy(state)))

    def test_empty_missing_malformed_and_unsupported_states_fail_atomically(self):
        with self.assertRaises(AdmissionDenied):
            self.service.migrate_legacy(migrated_at_utc=NOW)
        for state in (
            {**legacy_state(), "jobs": {"historical-job": {"receipts": {"bad": {"source_id": WIKIMEDIA_SOURCE_ID}}}}},
            {k: v for k, v in legacy_state().items() if k != "policy_digest"},
            {**legacy_state(), "local_smoke_source_counts": {WIKIMEDIA_SOURCE_ID: 0}},
            {**legacy_state(), "jobs": {"historical-job": {"receipts": {
                "a" * 64: {"source_id": WIKIMEDIA_SOURCE_ID, "pool": "serpapi", "reserved_at": NOW}}}}},
            {**legacy_state(), "schema": 99},
        ):
            self.put(state)
            before = self.get()
            with self.assertRaises(AdmissionDenied):
                self.service.migrate_legacy(migrated_at_utc=NOW)
            self.assertEqual(self.get(), before)

    def test_migration_preserves_full_legacy_state_and_is_idempotent(self):
        state = legacy_state(extra_source=True)
        self.put(state)
        self.assertEqual(self.service.migrate_legacy(migrated_at_utc=NOW), "MIGRATED")
        migrated = self.get()
        self.assertEqual(migrated["schema"], 2)
        self.assertEqual(migrated["state_identity"], "d6-smoke")
        self.assertEqual(migrated["legacy_state"], state)
        self.assertEqual(migrated["legacy_tranche"]["receipt_id"], "a" * 64)
        self.assertEqual(migrated["legacy_tranche"]["receipt"], state["jobs"]["historical-job"]["receipts"]["a" * 64])
        self.assertEqual(migrated["legacy_tranche"]["consumed_requests"], 1)
        self.assertEqual(migrated["legacy_tranche"]["policy_digest"], state["policy_digest"])
        self.assertEqual(migrated["legacy_tranche"]["state"], "CLOSED_CONSUMED")
        self.assertEqual(migrated["technical_capacity"]["authorized_capacity_requests"], 0)
        self.assertEqual(migrated["technical_capacity"]["state"], "INACTIVE")
        self.assertEqual(self.service.migrate_legacy(migrated_at_utc=NOW), "ALREADY_MIGRATED")
        self.assertEqual(self.get(), migrated)

    def test_missing_host_or_wrong_host_fails_closed(self):
        with self.assertRaises(ValueError):
            SameLedgerTranches(self.store, designated_host_id=" ")
        self.put(legacy_state())
        wrong = SameLedgerTranches(self.store, designated_host_id="windows-owner", host_id="linux")
        before = self.get()
        with self.assertRaises(AdmissionDenied):
            wrong.migrate_legacy(migrated_at_utc=NOW)
        self.assertEqual(self.get(), before)

    def test_live_profile_cannot_be_used_by_goal_migration_utility(self):
        with self.assertRaises(ValueError):
            SameLedgerTranches(LocalSQLiteAtomicStateStore.for_local_live(
                Path(self.temp.name) / "live-profile.sqlite"),
                designated_host_id="test-host", host_id="test-host")

    def test_partial_job_is_preserved_without_reinterpreting_failure(self):
        state = legacy_state(partial=True)
        self.put(state)
        self.service.migrate_legacy(migrated_at_utc=NOW)
        self.assertEqual(self.get()["legacy_state"]["jobs"]["historical-job"]["state"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
