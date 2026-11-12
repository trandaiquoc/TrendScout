import unittest
import tempfile
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.local_smoke_policy import local_smoke_policy
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID
from app.sources.live_gateway import GatewayError, SourceApproval, checked_source_approval
from app.storage.local_smoke_store import LocalSmokeStore
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.workers.guarded_attempt import GuardedAttemptRunner


class MemoryStore:
    def __init__(self):
        self.state = {}

    def transact(self, _key, operation):
        self.state, result = operation(self.state)
        return result


class LocalSmokePolicyTests(unittest.TestCase):
    def test_local_approval_cannot_claim_production_or_use_synthetic_store(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.sqlite"
            store = LocalSmokeStore(LocalSQLiteAtomicStateStore.for_local_live(path))
            policy = replace(local_smoke_policy(), enabled=True)
            runner = GuardedAttemptRunner(PublicAdmission(store, policy, state_key="local-test"))
            approval = SourceApproval(
                GOOGLE_SOURCE_ID, policy.plan_fingerprint, now + timedelta(minutes=5),
                "local-policy", approval_id="local-approval", source_enabled=True,
                global_enabled=True, usage_approved=True, retention_approved=True,
                quota_approved=True, cost_approved=True, security_approved=True,
                egress_controls_verified=True, telemetry_controls_verified=True,
                scope="LOCAL_SMOKE", local_controls_verified=True)

            class Approvals:
                def current(self, _source):
                    return approval

            args = (Approvals(), GOOGLE_SOURCE_ID, policy.plan_fingerprint, lambda: now)
            self.assertEqual(checked_source_approval(*args, runner=runner), approval)
            with self.assertRaises(GatewayError):
                checked_source_approval(*args)
            synthetic = LocalSmokeStore(LocalSQLiteAtomicStateStore(
                Path(directory) / "synthetic.sqlite", environment_profile="test"))
            synthetic_runner = GuardedAttemptRunner(PublicAdmission(synthetic, policy, state_key="test"))
            with self.assertRaises(GatewayError):
                checked_source_approval(*args, runner=synthetic_runner)
            with patch.dict(os.environ, {"VERCEL": "1"}):
                with self.assertRaises(GatewayError):
                    checked_source_approval(*args, runner=runner)
            production = replace(approval, scope="PRODUCTION")
            class ProductionApprovals:
                def current(self, _source):
                    return production
            with self.assertRaises(GatewayError):
                checked_source_approval(ProductionApprovals(), GOOGLE_SOURCE_ID,
                                        policy.plan_fingerprint, lambda: now, runner=runner)

    def test_live_storage_requires_explicit_factory_and_private_local_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.sqlite"
            with self.assertRaises(ValueError):
                LocalSQLiteAtomicStateStore(path, environment_profile="local", data_mode="LIVE")
            store = LocalSQLiteAtomicStateStore.for_local_live(path)
            self.assertEqual(store.data_mode, "LIVE")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch.dict(os.environ, {"VERCEL": "1"}):
                with self.assertRaises(ValueError):
                    LocalSQLiteAtomicStateStore.for_local_live(path)
            Path(directory).chmod(0o755)
            with self.assertRaises(ValueError):
                LocalSQLiteAtomicStateStore.for_local_live(path)
            Path(directory).chmod(0o700)

    def test_default_denies_and_shared_provider_attempt_budgets_are_bounded(self):
        policy = local_smoke_policy()
        self.assertFalse(policy.enabled)
        self.assertEqual({name: pool.daily_requests for name, pool in policy.pools.items()},
                         {"wikimedia": 1, "serpapi": 2, "youtube": 1})
        self.assertEqual(policy.max_attempts, 1)
        self.assertTrue(all(pool.max_inflight == 1 for pool in policy.pools.values()))
        admission = PublicAdmission(MemoryStore(), policy, state_key="local-smoke")
        with self.assertRaises(AdmissionDenied):
            admission.admit("smoke-policy-idempotency-key", now=datetime.now(timezone.utc))

    def test_persistent_source_cap_cannot_consume_other_engine_budget(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite"
            def admission():
                store = LocalSQLiteAtomicStateStore(path, environment_profile="test")
                return PublicAdmission(LocalSmokeStore(store), replace(local_smoke_policy(), enabled=True),
                                       state_key="smoke-test")
            first = admission()
            job = first.admit("smoke-policy-idempotency-key", now=now)["job_id"]
            fence = first.claim(job, "worker", now=now)["fence"]
            def reserve(control, source, attempt):
                return control.reserve_attempt(job, "worker", fence, source_id=source,
                                               attempt_id=attempt, now=now)
            self.assertEqual(reserve(first, GOOGLE_SOURCE_ID, "google-attempt-0001"), "NEW")
            self.assertEqual(reserve(first, GOOGLE_SOURCE_ID, "google-attempt-0001"), "REPLAY")
            first.settle_attempt(job, "worker", fence, attempt_id="google-attempt-0001", now=now)
            second = admission()
            with self.assertRaisesRegex(AdmissionDenied, "SMOKE_SOURCE_LIMIT"):
                reserve(second, GOOGLE_SOURCE_ID, "google-attempt-0002")
            self.assertEqual(reserve(second, BING_SOURCE_ID, "bing-attempt-0001"), "NEW")

    def test_concurrent_duplicate_source_reservations_allow_only_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite"
            LocalSQLiteAtomicStateStore(path, environment_profile="test")
            def reserve(index):
                store = LocalSmokeStore(LocalSQLiteAtomicStateStore(path, environment_profile="test"))
                def operation(state):
                    jobs = state.setdefault("jobs", {})
                    jobs[str(index)] = {"receipts": {"receipt": {"source_id": GOOGLE_SOURCE_ID}}}
                    return state, "NEW"
                try:
                    return store.transact("race", operation)
                except AdmissionDenied as exc:
                    return exc.code
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(reserve, (1, 2)))
            self.assertCountEqual(outcomes, ["NEW", "SMOKE_SOURCE_LIMIT"])


if __name__ == "__main__":
    unittest.main()
