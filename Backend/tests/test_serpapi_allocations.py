import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.collections.store import CollectionStore
from app.google_live_settings import GoogleLiveSettings
from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.normal_wikimedia_live_policy import GOOGLE_STATE_KEY, normal_google_live_policy
from app.orchestration.normal_google_live_composition import normal_google_live_composition
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore

NOW = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)


class SharedSerpApiAllocationTests(unittest.TestCase):
    def test_combined_policy_has_one_provider_ceiling_and_two_source_allocations(self):
        policy = normal_google_live_policy(bing_enabled=True)
        self.assertEqual(policy.enabled_sources, frozenset({
            "wikimedia_pageviews_top_per_country", GOOGLE_SOURCE_ID, BING_SOURCE_ID,
        }))
        self.assertEqual(policy.pools["serpapi"].monthly_requests, 244)
        self.assertEqual(policy.pools["serpapi_google"].monthly_requests, 122)
        self.assertEqual(policy.pools["serpapi_bing"].monthly_requests, 122)
        self.assertEqual(policy.source_allocations, {
            GOOGLE_SOURCE_ID: "serpapi_google", BING_SOURCE_ID: "serpapi_bing",
        })

    def test_policy_migration_preserves_google_and_seeds_allocation_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSQLiteAtomicStateStore.for_local_live(Path(directory) / "quota.sqlite")
            previous = normal_google_live_policy()
            updated = normal_google_live_policy(bing_enabled=True)
            old = PublicAdmission(store, previous, state_key=GOOGLE_STATE_KEY)
            new = PublicAdmission(store, updated, state_key=GOOGLE_STATE_KEY)
            original = {
                "schema": 1, "policy_digest": old.policy_digest, "operational_enabled": True,
                "jobs": {"a" * 32: {"job_id": "a" * 32, "state": "FAILED"}},
                "keys": {}, "starts": ["2026-11-16T07:00:00+00:00"],
                "quota": {
                    "serpapi:day:2026-11-16": {"requests": 4, "cost": 4},
                    "serpapi:month:2026-11": {"requests": 4, "cost": 4},
                    "google_rss:day:2026-11-16": {"requests": 2, "cost": 2},
                    "google_rss:month:2026-11": {"requests": 2, "cost": 2},
                },
                "inflight": {},
            }
            store.transact(GOOGLE_STATE_KEY, lambda _state: (original, None))
            audit = new.migrate_bounded_quota_policy(
                previous, authorization_note="OWNER_APPROVED_SHARED_BING_ALLOCATION",
                now=NOW,
            )
            self.assertTrue(audit["quota_preserved"])
            self.assertTrue(audit["jobs_preserved"])
            self.assertEqual(new.quota_snapshot("serpapi", now=NOW)["billing_cycle_status"],
                             "UNKNOWN")
            self.assertIsNone(new.quota_snapshot("serpapi", now=NOW)["monthly"]["remaining"])
            state = store.transact(GOOGLE_STATE_KEY, lambda value: (value, value))
            self.assertEqual(state["quota"]["serpapi:month:2026-11"], {"requests": 4, "cost": 4})
            self.assertEqual(state["quota"]["serpapi_google:month:2026-11"], {
                "requests": 4, "cost": 4,
            })
            self.assertEqual(state["jobs"], original["jobs"])
            self.assertEqual(state["starts"], original["starts"])
            new.set_serpapi_billing_cycle("2026-12-11", now=NOW)
            serpapi_monthly = new.quota_snapshot("serpapi", now=NOW)["monthly"]
            self.assertEqual(
                (serpapi_monthly["limit"], serpapi_monthly["used"],
                 serpapi_monthly["remaining"]),
                (244, 4, 240),
            )
            bing_monthly = new.quota_snapshot("serpapi_bing", now=NOW)["monthly"]
            self.assertEqual(
                (bing_monthly["limit"], bing_monthly["used"],
                 bing_monthly["remaining"]),
                (122, 0, 122),
            )
            self.assertEqual(new.quota_snapshot("serpapi", now=NOW)["billing_cycle_ends_at"],
                             "2026-12-11")

    def test_google_and_bing_attempts_share_provider_pool_and_track_allocations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSQLiteAtomicStateStore.for_local_live(Path(directory) / "quota.sqlite")
            policy = normal_google_live_policy(bing_enabled=True)
            admission = PublicAdmission(store, policy, state_key=GOOGLE_STATE_KEY)
            self.assertEqual(
                admission.quota_snapshot("serpapi", now=NOW)["billing_cycle_status"],
                "UNKNOWN",
            )
            self.assertIsNone(
                admission.quota_snapshot("serpapi", now=NOW)["monthly"]["remaining"]
            )
            with self.assertRaises(AdmissionDenied) as unknown_cycle:
                admission.set_serpapi_billing_cycle("2026-11-15", now=NOW)
            self.assertEqual(unknown_cycle.exception.code, "QUOTA_CYCLE_UNKNOWN")
            admission.set_serpapi_billing_cycle("2026-12-11", now=NOW)
            admitted = admission.admit(
                "shared-serpapi-fetch-key-0001", now=NOW,
                request_metadata={"sources": [GOOGLE_SOURCE_ID, BING_SOURCE_ID]},
            )
            lease = admission.claim(admitted["job_id"], "offline-worker", now=NOW)
            for source, attempt in ((GOOGLE_SOURCE_ID, "shared-google-attempt-00001"),
                                    (BING_SOURCE_ID, "shared-bing-attempt-000001")):
                admission.reserve_attempt(admitted["job_id"], "offline-worker", lease["fence"],
                                          source_id=source, attempt_id=attempt, now=NOW)
                admission.settle_attempt(admitted["job_id"], "offline-worker", lease["fence"],
                                         attempt_id=attempt, now=NOW)
            self.assertEqual(admission.quota_snapshot("serpapi", now=NOW)["daily"]["used"], 2)
            self.assertEqual(admission.quota_snapshot("serpapi_google", now=NOW)["daily"]["used"], 1)
            self.assertEqual(admission.quota_snapshot("serpapi_bing", now=NOW)["daily"]["used"], 1)

    def test_first_account_reconciliation_assigns_existing_usage_to_google(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSQLiteAtomicStateStore.for_local_live(Path(directory) / "quota.sqlite")
            previous = normal_google_live_policy()
            updated = normal_google_live_policy(bing_enabled=True)
            old = PublicAdmission(store, previous, state_key=GOOGLE_STATE_KEY)
            admission = PublicAdmission(store, updated, state_key=GOOGLE_STATE_KEY)
            state = {
                "schema": 1, "policy_digest": old.policy_digest, "operational_enabled": True,
                "jobs": {}, "keys": {}, "starts": [],
                "quota": {
                    "serpapi:day:2026-11-16": {"requests": 2, "cost": 2},
                    "serpapi:month:2026-11": {"requests": 2, "cost": 2},
                },
                "inflight": {},
            }
            store.transact(GOOGLE_STATE_KEY, lambda _state: (state, None))
            admission.migrate_bounded_quota_policy(
                previous, authorization_note="OWNER_APPROVED_SHARED_BING_ALLOCATION", now=NOW,
            )
            admission.set_serpapi_billing_cycle("2026-12-11", now=NOW)
            admission.reconcile_serpapi_account_usage("2026-12-11", 6, now=NOW)

            self.assertEqual(admission.quota_snapshot("serpapi", now=NOW)["monthly"]["used"], 6)
            self.assertEqual(
                admission.quota_snapshot("serpapi_google", now=NOW)["monthly"]["used"], 6
            )
            self.assertEqual(
                admission.quota_snapshot("serpapi_bing", now=NOW)["monthly"]["used"], 0
            )
            admission.reconcile_serpapi_account_usage("2026-12-11", 6, now=NOW)
            self.assertEqual(admission.quota_snapshot("serpapi", now=NOW)["monthly"]["used"], 6)

    def test_account_usage_above_local_ledger_fails_closed_after_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSQLiteAtomicStateStore.for_local_live(Path(directory) / "quota.sqlite")
            admission = PublicAdmission(
                store, normal_google_live_policy(bing_enabled=True), state_key=GOOGLE_STATE_KEY
            )
            admission.set_serpapi_billing_cycle("2026-12-11", now=NOW)
            admission.reconcile_serpapi_account_usage("2026-12-11", 6, now=NOW)
            with self.assertRaises(AdmissionDenied) as error:
                admission.reconcile_serpapi_account_usage("2026-12-11", 7, now=NOW)
            self.assertEqual(error.exception.code, "QUOTA_EXTERNAL_USAGE_UNRECONCILED")

    def test_provider_renewal_date_changes_monthly_window_without_calendar_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSQLiteAtomicStateStore.for_local_live(Path(directory) / "quota.sqlite")
            policy = normal_google_live_policy(bing_enabled=True)
            admission = PublicAdmission(store, policy, state_key=GOOGLE_STATE_KEY)
            admission.set_serpapi_billing_cycle("2026-12-11", now=NOW)
            admission.set_serpapi_billing_cycle(
                "2027-01-11", now=datetime(2026, 12, 11, 12, tzinfo=timezone.utc)
            )
            snapshot = admission.quota_snapshot(
                "serpapi", now=datetime(2026, 12, 11, 12, tzinfo=timezone.utc)
            )
            self.assertEqual(snapshot["monthly"]["used"], 0)
            self.assertEqual(snapshot["billing_cycle_ends_at"], "2027-01-11")

    def test_migration_fails_closed_when_existing_google_usage_exceeds_new_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalSQLiteAtomicStateStore.for_local_live(Path(directory) / "quota.sqlite")
            previous = normal_google_live_policy(serpapi_monthly_budget=180)
            updated = normal_google_live_policy(serpapi_monthly_budget=100, bing_enabled=True)
            old = PublicAdmission(store, previous, state_key=GOOGLE_STATE_KEY)
            new = PublicAdmission(store, updated, state_key=GOOGLE_STATE_KEY)
            state = {
                "schema": 1, "policy_digest": old.policy_digest, "operational_enabled": True,
                "jobs": {}, "keys": {}, "starts": [],
                "quota": {"serpapi:month:2026-11": {"requests": 101, "cost": 101}},
                "inflight": {},
            }
            store.transact(GOOGLE_STATE_KEY, lambda _state: (state, None))
            with self.assertRaises(AdmissionDenied) as error:
                new.migrate_bounded_quota_policy(
                    previous, authorization_note="OWNER_APPROVED_SHARED_BING_ALLOCATION",
                    now=NOW,
                )
            self.assertEqual(error.exception.code, "POLICY_MIGRATION_QUOTA_INCOMPATIBLE")
            unchanged = store.transact(GOOGLE_STATE_KEY, lambda value: (value, value))
            self.assertEqual(unchanged["policy_digest"], old.policy_digest)
            self.assertEqual(unchanged["quota"], state["quota"])

    def test_combined_live_composition_migrates_existing_google_state_without_searching(self):
        import asyncio

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state_dir = root / "google-state"
                state_dir.mkdir(mode=0o700)
                local_state = LocalSQLiteAtomicStateStore.for_local_live(
                    state_dir / "state.sqlite"
                )
                previous = normal_google_live_policy()
                prior_admission = PublicAdmission(
                    local_state, previous, state_key=GOOGLE_STATE_KEY
                )
                prior_state = {
                    "schema": 1, "policy_digest": prior_admission.policy_digest,
                    "operational_enabled": True, "jobs": {}, "keys": {}, "starts": [],
                    "quota": {
                        "serpapi:day:2026-11-16": {"requests": 4, "cost": 4},
                        "serpapi:month:2026-11": {"requests": 4, "cost": 4},
                    },
                    "inflight": {},
                }
                local_state.transact(GOOGLE_STATE_KEY, lambda _state: (prior_state, None))
                collection_store = CollectionStore(
                    root / "collections.sqlite", data_mode_scope="LIVE"
                )

                class FakeCredentials:
                    def resolve(self, reference):
                        return "offline-serpapi-key" if reference == "SERPAPI_API_KEY" else None

                config = GoogleLiveSettings(
                    bing_approved=True,
                    policy_migration_approved=True,
                    account_api_approved=True,
                )
                async with normal_google_live_composition(
                    user_agent="TrendScoutTest/1.0 (https://example.invalid/contact)",
                    state_directory=state_dir,
                    google_settings=config,
                    credential_provider=FakeCredentials(),
                    collection_store=collection_store,
                    utcnow=lambda: NOW,
                ) as facade:
                    self.assertTrue(facade.normal_bing_live)
                    self.assertIn(BING_SOURCE_ID, facade.admission.policy.enabled_sources)
                    self.assertEqual(
                        facade.google_quota_metadata()["serpapi"]["billing_cycle_status"],
                        "UNKNOWN",
                    )
                    self.assertIsNone(
                        facade.google_quota_metadata()["serpapi_google"]
                        ["monthly"]["remaining"]
                    )
                    state = local_state.transact(
                        GOOGLE_STATE_KEY, lambda value: (value, value)
                    )
                    self.assertEqual(len(state["policy_migration_audit"]), 2)
                    self.assertEqual(state["quota"]["serpapi:month:2026-11"], {
                        "requests": 4, "cost": 4,
                    })

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
