"""Bounded Vercel-triggered reconciliation for the existing Firestore ledger."""

import unittest
from copy import deepcopy
from datetime import datetime, timezone

from app.google_live_settings import GoogleLiveSettings
from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.normal_wikimedia_live_policy import GOOGLE_STATE_KEY, normal_google_live_policy
from app.orchestration.vercel_wikimedia_live_composition import (
    _vercel_previous_policy_candidates,
    _VercelWikimediaPolicySync,
)

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


class _MemoryAtomicStateStore:
    def __init__(self, state):
        self.state = deepcopy(state)
        self.writes = 0
        self.read_many_calls = 0
        self.transact_calls = 0

    def transact(self, key, operation):
        self.transact_calls += 1
        if key != GOOGLE_STATE_KEY:
            raise AssertionError("unexpected state key")
        updated, result = operation(deepcopy(self.state))
        if updated != self.state:
            self.state = deepcopy(updated)
            self.writes += 1
        return deepcopy(result)

    def read_many(self, keys):
        self.read_many_calls += 1
        if keys != [GOOGLE_STATE_KEY]:
            raise AssertionError("unexpected state keys")
        return {GOOGLE_STATE_KEY: deepcopy(self.state)}


class VercelWikimediaPolicySyncTests(unittest.TestCase):
    def setUp(self):
        self.settings = GoogleLiveSettings()
        self.target_policy = normal_google_live_policy(
            serpapi_daily_budget=self.settings.serpapi_daily_budget,
            serpapi_monthly_budget=self.settings.serpapi_monthly_budget,
            rss_daily_budget=self.settings.rss_daily_budget,
            rss_monthly_budget=self.settings.rss_monthly_budget,
            fetch_starts_per_hour=self.settings.fetch_starts_per_hour,
            bing_daily_budget=self.settings.bing_daily_budget,
            bing_enabled=True,
            youtube_enabled=True,
        )
        self.previous = _vercel_previous_policy_candidates(self.settings)

    def sync(self, store):
        return _VercelWikimediaPolicySync(
            store,
            target_policy=self.target_policy,
            previous_policies=self.previous,
            state_key=GOOGLE_STATE_KEY,
            utcnow=lambda: NOW,
        )

    @staticmethod
    def state_for(policy, *, operational_enabled=True, quota=None):
        admission = PublicAdmission(
            _MemoryAtomicStateStore({}), policy, state_key=GOOGLE_STATE_KEY
        )
        return {
            "schema": 1,
            "policy_digest": admission.policy_digest,
            "operational_enabled": operational_enabled,
            "jobs": {"existing-job": {"state": "COMPLETED"}},
            "keys": {"existing-key": {"job_id": "existing-job"}},
            "starts": ["2026-11-19T11:00:00+00:00"],
            "quota": quota or {
                "serpapi:day:2026-11-19": {"requests": 2, "cost": 2},
                "serpapi:month:2026-11": {"requests": 2, "cost": 2},
                "google_rss:day:2026-11-19": {"requests": 1, "cost": 1},
                "google_rss:month:2026-11": {"requests": 1, "cost": 1},
            },
            "inflight": {},
            "policy_migration_audit": [],
        }

    def test_known_old_policy_syncs_atomically_and_preserves_existing_ledger(self):
        old_policy = self.previous["previous_without_bing_or_youtube"]
        initial = self.state_for(old_policy)
        store = _MemoryAtomicStateStore(initial)
        sync = self.sync(store)

        self.assertEqual(sync.status(), "READY_TO_SYNC")
        self.assertEqual(store.read_many_calls, 1)
        self.assertEqual(store.transact_calls, 0)
        self.assertEqual(sync.prepare_for_fetch(), "RECONCILED")
        self.assertEqual(store.writes, 1)
        self.assertEqual(
            store.state["policy_digest"], PublicAdmission._policy_digest(self.target_policy)
        )
        self.assertTrue(store.state["operational_enabled"])
        self.assertEqual(store.state["jobs"], initial["jobs"])
        self.assertEqual(store.state["keys"], initial["keys"])
        self.assertEqual(store.state["starts"], initial["starts"])
        self.assertEqual(store.state["quota"]["serpapi:day:2026-11-19"],
                         initial["quota"]["serpapi:day:2026-11-19"])
        self.assertEqual(store.state["quota"]["serpapi_google:day:2026-11-19"],
                         initial["quota"]["serpapi:day:2026-11-19"])
        self.assertEqual(len(store.state["policy_migration_audit"]), 1)
        self.assertTrue(store.state["policy_migration_audit"][0]["quota_preserved"])
        self.assertTrue(store.state["policy_migration_audit"][0]["jobs_preserved"])
        self.assertTrue(sync.target.operationally_enabled())

    def test_unknown_policy_and_disabled_kill_switch_fail_closed_without_write(self):
        unknown_store = _MemoryAtomicStateStore({
            **self.state_for(self.previous["previous_without_bing_or_youtube"]),
            "policy_digest": "f" * 64,
        })
        unknown_sync = self.sync(unknown_store)
        self.assertEqual(unknown_sync.status(), "POLICY_MISMATCH")
        with self.assertRaises(AdmissionDenied) as error:
            unknown_sync.prepare_for_fetch()
        self.assertEqual(error.exception.code, "POLICY_MISMATCH")
        self.assertEqual(unknown_store.writes, 0)

        disabled_store = _MemoryAtomicStateStore(self.state_for(
            self.previous["previous_without_bing_or_youtube"], operational_enabled=False
        ))
        disabled_sync = self.sync(disabled_store)
        self.assertEqual(disabled_sync.status(), "FEATURE_DISABLED")
        with self.assertRaises(AdmissionDenied) as error:
            disabled_sync.prepare_for_fetch()
        self.assertEqual(error.exception.code, "FEATURE_DISABLED")
        self.assertEqual(disabled_store.writes, 0)

    def test_quota_incompatible_legacy_state_is_not_modified(self):
        old_policy = self.previous["legacy_low_budget_without_bing_or_youtube"]
        original = self.state_for(old_policy, quota={
            "serpapi:day:2026-11-19": {"requests": 7, "cost": 7},
            "serpapi:month:2026-11": {"requests": 7, "cost": 7},
            "google_rss:day:2026-11-19": {"requests": 1, "cost": 1},
            "google_rss:month:2026-11": {"requests": 1, "cost": 1},
        })
        store = _MemoryAtomicStateStore(original)
        sync = self.sync(store)

        with self.assertRaises(AdmissionDenied) as error:
            sync.prepare_for_fetch()

        self.assertEqual(error.exception.code, "POLICY_MIGRATION_QUOTA_INCOMPATIBLE")
        self.assertEqual(store.state, original)
        self.assertEqual(store.writes, 0)

    def test_capacity_one_and_new_source_pools_are_reconciled_in_one_transaction(self):
        old_policy = self.previous["previous_capacity_one_without_bing_or_youtube"]
        initial = self.state_for(old_policy)
        store = _MemoryAtomicStateStore(initial)
        sync = self.sync(store)

        self.assertEqual(sync.prepare_for_fetch(), "RECONCILED")
        self.assertEqual(store.writes, 1)
        audit = store.state["policy_migration_audit"][0]
        self.assertEqual(audit["changed_admission_limits"], [
            "max_starts_per_window", "enabled_sources",
        ])
        self.assertEqual(store.state["starts"], initial["starts"])


if __name__ == "__main__":
    unittest.main()
