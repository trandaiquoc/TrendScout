"""Multi-process offline admission tests over the local SQLite state store."""

import multiprocessing
import os
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.jobs.public_admission import (
    YOUTUBE_OTHER_POOL,
    AdmissionDenied,
    AdmissionPolicy,
    PoolLimit,
    PublicAdmission,
)
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, YOUTUBE_SOURCE_ID
from app.storage.local_file_security import secure_private_directory, verify_private_file
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


class SqliteTestStore:
    """Test profile over the real local atomic-store implementation."""

    def __init__(self, path):
        self.store = LocalSQLiteAtomicStateStore(path, environment_profile="test")

    def transact(self, key, operation):
        return self.store.transact(key, operation)


def policy(**changes):
    params = {"enabled_sources": frozenset({GOOGLE_SOURCE_ID, BING_SOURCE_ID, YOUTUBE_SOURCE_ID}),
              "pools": {"serpapi": PoolLimit(2, 5, 2, 5, 1),
                        "youtube": PoolLimit(1, 3, 1, 3, 1)},
              "plan_fingerprint": "a" * 64, "cooldown_seconds": 30, "lease_seconds": 10,
              "enabled": True}
    params.update(changes)
    return AdmissionPolicy(**params)


def coordinator(path, config=None):
    return PublicAdmission(SqliteTestStore(path), config or policy(), state_key="public")


def admit_worker(args):
    path, index = args
    return coordinator(path).admit(f"independent-client-{index:04d}", now=NOW)


def reserve_worker(args):
    path, job_id, fence, index = args
    try:
        return coordinator(path).reserve_attempt(job_id, "worker", fence,
            source_id=GOOGLE_SOURCE_ID if index % 2 else BING_SOURCE_ID,
            attempt_id=f"physical-attempt-{index:04d}", now=NOW)
    except AdmissionDenied as exc:
        return exc.code


def reserve_concurrency_worker(args):
    path, job_id, fence, index = args
    config = policy(pools={"serpapi": PoolLimit(100, 100, 100, 100, 1, 2, 30),
                           "youtube": PoolLimit(100, 100, 100, 100, 1, 2, 30)})
    try:
        return coordinator(path, config).reserve_attempt(
            job_id, "worker", fence, source_id=GOOGLE_SOURCE_ID if index % 2 else BING_SOURCE_ID,
            attempt_id=f"physical-attempt-{index:04d}", now=NOW)
    except AdmissionDenied as exc:
        return exc.code


def reserve_then_crash(path, job_id, fence):
    coordinator(path).reserve_attempt(job_id, "worker", fence, source_id=GOOGLE_SOURCE_ID,
                                      attempt_id="crashed-physical-attempt", now=NOW)
    os._exit(31)


class PublicAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        if os.name == "nt":
            secure_private_directory(self.directory.name)
        self.path = str(Path(self.directory.name) / "coordinator.sqlite")
        self.control = coordinator(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_history_request_metadata_is_idempotent_and_survives_worker_claim(self):
        metadata = {"kind": "WIKIMEDIA_HISTORY", "history_days": 2}
        accepted = self.control.admit("explicit-history-request", now=NOW,
                                      request_metadata=metadata)
        replay = self.control.admit("explicit-history-request", now=NOW,
                                    request_metadata=metadata)
        self.assertEqual(replay["result"], "REPLAY")
        with self.assertRaisesRegex(AdmissionDenied, "IDEMPOTENCY_KEY_CONFLICT"):
            self.control.admit("explicit-history-request", now=NOW,
                               request_metadata={"kind": "WIKIMEDIA_HISTORY", "history_days": 1})
        claim = self.control.claim_next("history-worker", now=NOW)
        self.assertEqual(claim["job_id"], accepted["job_id"])
        self.assertEqual(claim["request_metadata"], metadata)
        with self.assertRaises(ValueError):
            self.control.admit("oversized-history-request", now=NOW,
                               request_metadata={"kind": "WIKIMEDIA_HISTORY", "history_days": 3})

    def test_independent_processes_coalesce_and_share_provider_budget(self):
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=8, mp_context=context) as processes:
            results = list(processes.map(admit_worker, [(self.path, i) for i in range(20)]))
        self.assertEqual(sum(item["result"] == "CREATED" for item in results), 1)
        self.assertEqual(len({item["job_id"] for item in results}), 1)
        job_id = results[0]["job_id"]
        fence = self.control.claim(job_id, "worker", now=NOW)["fence"]
        with ProcessPoolExecutor(max_workers=8, mp_context=context) as processes:
            results = list(processes.map(reserve_worker,
                           [(self.path, job_id, fence, i) for i in range(20)]))
        self.assertEqual(results.count("NEW"), 2)
        self.assertEqual(results.count("QUOTA_EXHAUSTED"), 18)

    def test_reopened_store_keeps_committed_state_and_rolls_back_failed_transform(self):
        self.control.store.transact("durable-check", lambda _state: ({"count": 1}, None))
        reopened = SqliteTestStore(self.path)

        def abort(_state):
            raise RuntimeError("abort transaction")

        with self.assertRaisesRegex(RuntimeError, "abort transaction"):
            reopened.transact("durable-check", abort)
        self.assertEqual(
            reopened.transact("durable-check", lambda state: (state, state["count"])), 1
        )

    def test_local_store_refuses_live_or_ephemeral_composition(self):
        for kwargs in (
            {"path": self.path, "environment_profile": "production"},
            {"path": self.path, "environment_profile": "local", "data_mode": "LIVE"},
            {"path": ":memory:", "environment_profile": "test"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                LocalSQLiteAtomicStateStore(**kwargs)

    def test_local_database_file_is_owner_only(self):
        if os.name == "nt":
            verify_private_file(self.control.store.store.path)
        else:
            self.assertEqual(self.control.store.store.path.stat().st_mode & 0o077, 0)

    def test_independent_processes_share_inflight_limit(self):
        config = policy(pools={"serpapi": PoolLimit(100, 100, 100, 100, 1, 2, 30),
                               "youtube": PoolLimit(100, 100, 100, 100, 1, 2, 30)})
        control = coordinator(self.path, config)
        job = control.admit("one-request-idempotency", now=NOW)["job_id"]
        fence = control.claim(job, "worker", now=NOW)["fence"]
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=8, mp_context=context) as processes:
            results = list(processes.map(reserve_concurrency_worker,
                           [(self.path, job, fence, i) for i in range(20)]))
        self.assertEqual(results.count("NEW"), 2)
        self.assertEqual(results.count("PROVIDER_CONCURRENCY_LIMIT"), 18)

    def test_duplicate_reservation_is_not_permission_to_send_again(self):
        job = self.control.admit("one-request-idempotency", now=NOW)["job_id"]
        fence = self.control.claim(job, "worker", now=NOW)["fence"]
        args = {"source_id": GOOGLE_SOURCE_ID, "attempt_id": "one-physical-attempt", "now": NOW}
        self.assertEqual(self.control.reserve_attempt(job, "worker", fence, **args), "NEW")
        self.assertEqual(self.control.reserve_attempt(job, "worker", fence, **args), "REPLAY")
        with self.assertRaisesRegex(AdmissionDenied, "ATTEMPT_ID_CONFLICT"):
            self.control.reserve_attempt(job, "worker", fence, **{**args, "source_id": BING_SOURCE_ID})

    def test_owner_manual_youtube_bypasses_only_internal_caps_and_keeps_ledger(self):
        config = policy(max_starts_per_window=10, cooldown_seconds=0)
        regular = coordinator(self.path, config)
        manual = PublicAdmission(
            SqliteTestStore(self.path), config, state_key="public",
            owner_manual_youtube_live=True,
        )
        self.assertEqual(manual.policy_digest, regular.policy_digest)

        first_job = regular.admit("normal-youtube-fetch-key-0001", now=NOW,
                                  request_metadata={"sources": [YOUTUBE_SOURCE_ID]})["job_id"]
        first_fence = regular.claim(first_job, "worker", now=NOW)["fence"]
        regular.reserve_attempt(
            first_job, "worker", first_fence, source_id=YOUTUBE_SOURCE_ID,
            attempt_id="normal-physical-attempt-0001", now=NOW,
        )
        with self.assertRaisesRegex(AdmissionDenied, "QUOTA_EXHAUSTED"):
            regular.reserve_attempt(
                first_job, "worker", first_fence, source_id=YOUTUBE_SOURCE_ID,
                attempt_id="normal-physical-attempt-0002", now=NOW,
            )
        regular.settle_attempt(
            first_job, "worker", first_fence,
            attempt_id="normal-physical-attempt-0001", now=NOW,
        )
        regular.finish(first_job, "worker", first_fence, now=NOW)

        for index in range(4):
            now = NOW + timedelta(seconds=index + 1)
            job_id = manual.admit(
                f"manual-youtube-fetch-key-{index:04d}", now=now,
                request_metadata={"sources": [YOUTUBE_SOURCE_ID]},
            )["job_id"]
            fence = manual.claim(job_id, f"manual-worker-{index}", now=now)["fence"]
            attempt_id = f"manual-physical-attempt-{index:04d}"
            manual.reserve_attempt(
                job_id, f"manual-worker-{index}", fence, source_id=YOUTUBE_SOURCE_ID,
                attempt_id=attempt_id, now=now,
            )
            manual.settle_attempt(
                job_id, f"manual-worker-{index}", fence, attempt_id=attempt_id, now=now,
            )
            manual.finish(job_id, f"manual-worker-{index}", fence, now=now)

        snapshot = manual.quota_snapshot("youtube", now=NOW + timedelta(seconds=5))
        self.assertEqual(snapshot["daily"]["used"], 5)
        self.assertEqual(snapshot["monthly"]["used"], 5)
        self.assertEqual(snapshot["daily"]["remaining"], 0)

    def test_expired_lease_fences_old_worker_and_preserves_receipt(self):
        job = self.control.admit("one-request-idempotency", now=NOW)["job_id"]
        old = self.control.claim(job, "worker", now=NOW)["fence"]
        self.control.reserve_attempt(job, "worker", old, source_id=GOOGLE_SOURCE_ID,
                                     attempt_id="one-physical-attempt", now=NOW)
        later = NOW + timedelta(seconds=11)
        new = self.control.claim(job, "worker2", now=later)["fence"]
        self.assertGreater(new, old)
        with self.assertRaisesRegex(AdmissionDenied, "LEASE_LOST"):
            self.control.finish(job, "worker", old, now=later)
        self.assertEqual(self.control.reserve_attempt(job, "worker2", new,
            source_id=GOOGLE_SOURCE_ID, attempt_id="one-physical-attempt", now=later), "REPLAY")
        self.control.finish(job, "worker2", new, now=later)

    def test_status_and_history_requeue_an_expired_lease_without_running_a_worker(self):
        job_id = self.control.admit("one-request-idempotency", now=NOW)["job_id"]
        self.control.claim(job_id, "worker", now=NOW)
        later = NOW + timedelta(seconds=11)

        status = self.control.read_job(job_id, now=later)
        self.assertEqual(status["state"], "QUEUED")
        self.assertTrue(status["recovery_available"])
        self.assertIsNone(status["owner"])
        self.assertIsNone(status["lease_until"])

        history = self.control.list_jobs(limit=20, now=later)
        self.assertEqual(history[0]["state"], "QUEUED")
        self.assertTrue(history[0]["recovery_available"])

        # A later explicit POST can join and dispatch the queued durable job.
        joined = self.control.admit("explicit-recovery-request", now=later)
        self.assertEqual(joined["job_id"], job_id)
        self.assertEqual(joined["result"], "JOINED")
        reclaimed = self.control.claim_next("recovery-worker", now=later)
        self.assertEqual(reclaimed["job_id"], job_id)
        self.assertEqual(reclaimed["fence"], 2)

    def test_cooldown_kill_switch_and_mismatched_policy_fail_closed(self):
        job = self.control.admit("one-request-idempotency", now=NOW)["job_id"]
        fence = self.control.claim(job, "worker", now=NOW)["fence"]
        self.control.finish(job, "worker", fence, now=NOW)
        with self.assertRaisesRegex(AdmissionDenied, "COOLDOWN"):
            self.control.admit("another-idempotency-key", now=NOW + timedelta(seconds=1))
        with self.assertRaisesRegex(AdmissionDenied, "POLICY_MISMATCH"):
            coordinator(self.path, policy(cooldown_seconds=0)).admit("another-idempotency-key", now=NOW)
        self.control.disable()
        with self.assertRaisesRegex(AdmissionDenied, "FEATURE_DISABLED"):
            self.control.admit("another-idempotency-key", now=NOW + timedelta(seconds=31))

    def test_clock_regression_and_disabled_policy_fail_closed(self):
        job = self.control.admit("one-request-idempotency", now=NOW)["job_id"]
        with self.assertRaisesRegex(AdmissionDenied, "CLOCK_REGRESSION"):
            self.control.claim(job, "worker", now=NOW - timedelta(seconds=1))
        with self.assertRaisesRegex(AdmissionDenied, "FEATURE_DISABLED"):
            coordinator(self.path, policy(enabled=False)).admit("different-idempotency-key", now=NOW)

    def test_durable_queue_defers_without_shortening_retry_after(self):
        job = self.control.admit("one-request-idempotency", now=NOW)["job_id"]
        claimed = self.control.claim_next("worker", now=NOW)
        self.assertEqual(claimed["job_id"], job)
        retry_at = NOW + timedelta(seconds=40)
        self.control.defer(job, "worker", claimed["fence"], now=NOW, not_before=retry_at)
        self.assertIsNone(self.control.claim_next("worker2", now=NOW + timedelta(seconds=39)))
        next_claim = self.control.claim_next("worker2", now=retry_at)
        self.assertGreater(next_claim["fence"], claimed["fence"])
        with self.assertRaisesRegex(AdmissionDenied, "LEASE_LOST"):
            self.control.finish(job, "worker", claimed["fence"], now=retry_at)

    def test_unknown_commit_receipt_cannot_allow_a_second_attempt(self):
        job = self.control.admit("one-request-idempotency", now=NOW)["job_id"]
        fence = self.control.claim(job, "worker", now=NOW)["fence"]
        args = {"source_id": GOOGLE_SOURCE_ID, "attempt_id": "one-physical-attempt", "now": NOW}
        # Simulate a commit whose acknowledgement was lost. The next invocation
        # finds the durable receipt, which is not permission to send the request.
        self.assertEqual(self.control.reserve_attempt(job, "worker", fence, **args), "NEW")
        self.assertEqual(self.control.reserve_attempt(job, "worker", fence, **args), "REPLAY")

    def test_process_death_after_reservation_keeps_durable_receipt(self):
        job = self.control.admit("one-request-idempotency", now=NOW)["job_id"]
        fence = self.control.claim(job, "worker", now=NOW)["fence"]
        context = multiprocessing.get_context("spawn")
        child = context.Process(target=reserve_then_crash, args=(self.path, job, fence))
        child.start()
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join()
            self.fail("offline child process did not stop")
        self.assertEqual(child.exitcode, 31)
        later = NOW + timedelta(seconds=11)
        successor = self.control.claim_next("worker2", now=later)
        self.assertEqual(self.control.reserve_attempt(job, "worker2", successor["fence"],
            source_id=GOOGLE_SOURCE_ID, attempt_id="crashed-physical-attempt", now=later), "REPLAY")

    def test_pool_configuration_is_copied_at_construction(self):
        config = policy()
        control = coordinator(self.path, config)
        job = control.admit("one-request-idempotency", now=NOW)["job_id"]
        fence = control.claim(job, "worker", now=NOW)["fence"]
        config.pools["serpapi"] = PoolLimit(100, 100, 100, 100, 1)
        for suffix in ("first", "second"):
            self.assertEqual(control.reserve_attempt(job, "worker", fence,
                source_id=GOOGLE_SOURCE_ID, attempt_id="physical-attempt-" + suffix, now=NOW), "NEW")
        with self.assertRaisesRegex(AdmissionDenied, "QUOTA_EXHAUSTED"):
            control.reserve_attempt(job, "worker", fence, source_id=BING_SOURCE_ID,
                                    attempt_id="physical-attempt-third", now=NOW)

    def test_provider_concurrency_survives_worker_lease_expiry(self):
        config = policy(pools={"serpapi": PoolLimit(10, 10, 10, 10, 1, 1, 30),
                               "youtube": PoolLimit(10, 10, 10, 10, 1, 1, 30)})
        control = coordinator(self.path, config)
        job = control.admit("one-request-idempotency", now=NOW)["job_id"]
        fence = control.claim(job, "worker", now=NOW)["fence"]
        control.reserve_attempt(job, "worker", fence, source_id=GOOGLE_SOURCE_ID,
                                attempt_id="physical-attempt-first", now=NOW)
        later = NOW + timedelta(seconds=11)
        new = control.claim(job, "worker2", now=later)["fence"]
        with self.assertRaisesRegex(AdmissionDenied, "PROVIDER_CONCURRENCY_LIMIT"):
            control.reserve_attempt(job, "worker2", new, source_id=BING_SOURCE_ID,
                                    attempt_id="physical-attempt-second", now=later)
        self.assertEqual(control.reserve_attempt(job, "worker2", new, source_id=YOUTUBE_SOURCE_ID,
                         attempt_id="youtube-physical-attempt", now=later), "NEW")

    def test_settle_frees_slot_without_refunding_quota_and_heartbeat_is_fenced(self):
        config = policy(pools={"serpapi": PoolLimit(2, 5, 2, 5, 1, 1, 30),
                               "youtube": PoolLimit(1, 3, 1, 3, 1)})
        control = coordinator(self.path, config)
        job = control.admit("one-request-idempotency", now=NOW)["job_id"]
        fence = control.claim(job, "worker", now=NOW)["fence"]
        control.reserve_attempt(job, "worker", fence, source_id=GOOGLE_SOURCE_ID,
                                attempt_id="physical-attempt-first", now=NOW)
        self.assertTrue(control.settle_attempt(job, "worker", fence,
                        attempt_id="physical-attempt-first", now=NOW))
        self.assertFalse(control.settle_attempt(job, "worker", fence,
                         attempt_id="physical-attempt-first", now=NOW))
        control.reserve_attempt(job, "worker", fence, source_id=BING_SOURCE_ID,
                                attempt_id="physical-attempt-second", now=NOW)
        with self.assertRaisesRegex(AdmissionDenied, "QUOTA_EXHAUSTED"):
            control.reserve_attempt(job, "worker", fence, source_id=BING_SOURCE_ID,
                                    attempt_id="physical-attempt-third", now=NOW)
        self.assertEqual(control.heartbeat(job, "worker", fence, now=NOW + timedelta(seconds=9)),
                         (NOW + timedelta(seconds=19)).isoformat())
        with self.assertRaisesRegex(AdmissionDenied, "LEASE_LOST"):
            control.heartbeat(job, "worker", fence + 1, now=NOW + timedelta(seconds=9))

    def test_daily_and_monthly_budget_windows_are_independent(self):
        config = policy(pools={"serpapi": PoolLimit(1, 2, 1, 2, 1),
                               "youtube": PoolLimit(1, 3, 1, 3, 1)})
        control = coordinator(self.path, config)
        def run_at(when, index):
            job = control.admit(f"window-request-{index:04d}", now=when)["job_id"]
            fence = control.claim(job, "worker", now=when)["fence"]
            return job, fence
        job, fence = run_at(NOW, 1)
        control.reserve_attempt(job, "worker", fence, source_id=GOOGLE_SOURCE_ID,
                                attempt_id="window-attempt-0001", now=NOW)
        control.finish(job, "worker", fence, now=NOW)
        day2 = NOW + timedelta(days=1)
        job, fence = run_at(day2, 2)
        control.reserve_attempt(job, "worker", fence, source_id=BING_SOURCE_ID,
                                attempt_id="window-attempt-0002", now=day2)
        control.finish(job, "worker", fence, now=day2)
        day3 = NOW + timedelta(days=2)
        job, fence = run_at(day3, 3)
        with self.assertRaisesRegex(AdmissionDenied, "QUOTA_EXHAUSTED"):
            control.reserve_attempt(job, "worker", fence, source_id=GOOGLE_SOURCE_ID,
                                    attempt_id="window-attempt-0003", now=day3)
        december = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
        job, fence = run_at(december, 4)
        self.assertEqual(control.reserve_attempt(job, "worker", fence, source_id=GOOGLE_SOURCE_ID,
                         attempt_id="window-attempt-0004", now=december), "NEW")

    def test_old_quota_windows_are_pruned_without_erasing_unknown_or_future_buckets(self):
        config = policy(pools={"serpapi": PoolLimit(20, 40, 20, 40, 1),
                               "youtube": PoolLimit(20, 40, 20, 40, 1)})
        control = coordinator(self.path, config)

        def reserve_at(when, index):
            job = control.admit(f"ledger-window-request-{index:04d}", now=when)["job_id"]
            fence = control.claim(job, "worker", now=when)["fence"]
            control.reserve_attempt(job, "worker", fence, source_id=GOOGLE_SOURCE_ID,
                                    attempt_id=f"ledger-window-attempt-{index:04d}", now=when)
            control.finish(job, "worker", fence, now=when)

        reserve_at(NOW, 1)

        def add_untrusted_entries(state):
            state["quota"]["unknown:day:2020-01-01"] = {"requests": 7, "cost": 7}
            state["quota"]["serpapi:month:bad-month"] = {"requests": 8, "cost": 8}
            state["quota"]["serpapi:day:2028-01-01"] = {"requests": 9, "cost": 9}
            return state, None

        control.store.transact("public", add_untrusted_entries)
        january = datetime(2027, 1, 2, 12, tzinfo=timezone.utc)
        reserve_at(january, 2)
        quota = control.store.transact("public", lambda state: (state, state["quota"]))
        self.assertNotIn("serpapi:day:2026-11-12", quota)
        self.assertNotIn("serpapi:month:2026-11", quota)
        self.assertEqual(quota["serpapi:day:2027-01-02"]["requests"], 1)
        self.assertEqual(quota["serpapi:month:2027-01"]["requests"], 1)
        self.assertIn("unknown:day:2020-01-01", quota)
        self.assertIn("serpapi:month:bad-month", quota)
        self.assertIn("serpapi:day:2028-01-01", quota)

    def test_youtube_quota_pools_are_separate_and_month_ceiling_is_enforced(self):
        pools = {
            "serpapi": PoolLimit(2, 5, 2, 5, 1),
            "youtube": PoolLimit(6, 200, 6, 200, 1),
            YOUTUBE_OTHER_POOL: PoolLimit(6, 200, 6, 200, 1),
        }
        config = policy(pools=pools)
        control = coordinator(self.path, config)
        job = control.admit("youtube-month-budget-test", now=NOW)["job_id"]
        fence = control.claim(job, "worker", now=NOW)["fence"]

        def set_search_usage(state):
            state["quota"]["youtube:month:2026-11"] = {"requests": 199, "cost": 199}
            return state, None

        control.store.transact("public", set_search_usage)
        self.assertEqual(control.quota_snapshot("youtube", now=NOW)["monthly"]["used"], 199)
        self.assertEqual(control.quota_snapshot(YOUTUBE_OTHER_POOL, now=NOW)["monthly"]["used"], 0)

        self.assertEqual(control.reserve_attempt(
            job, "worker", fence, source_id=YOUTUBE_SOURCE_ID,
            attempt_id="youtube-month-search-attempt", now=NOW,
        ), "NEW")
        control.record_provider_request_started(
            job, "worker", fence, source_id=YOUTUBE_SOURCE_ID,
            attempt_id="youtube-month-search-attempt", now=NOW,
        )
        self.assertEqual(control.last_provider_request_at(YOUTUBE_SOURCE_ID), NOW.isoformat())
        control.settle_attempt(job, "worker", fence,
                               attempt_id="youtube-month-search-attempt", now=NOW)
        self.assertEqual(control.quota_snapshot("youtube", now=NOW)["monthly"]["used"], 200)
        with self.assertRaisesRegex(AdmissionDenied, "QUOTA_EXHAUSTED"):
            control.reserve_attempt(
                job, "worker", fence, source_id=YOUTUBE_SOURCE_ID,
                attempt_id="youtube-month-search-attempt-overflow", now=NOW,
            )

        self.assertEqual(control.reserve_attempt(
            job, "worker", fence, source_id=YOUTUBE_SOURCE_ID,
            pool_name=YOUTUBE_OTHER_POOL,
            attempt_id="youtube-other-method-attempt", now=NOW,
        ), "NEW")
        other = control.quota_snapshot(YOUTUBE_OTHER_POOL, now=NOW)
        self.assertEqual(other["daily"]["used"], 1)
        self.assertEqual(other["monthly"]["used"], 1)
        search = control.quota_snapshot("youtube", now=NOW)
        self.assertEqual(search["daily"]["used"], 1)
        self.assertEqual(search["monthly"]["used"], 200)
        self.assertEqual(search["daily"]["timezone"], "America/Los_Angeles")
        self.assertEqual(search["monthly"]["timezone"], "UTC")

    def test_youtube_provider_reset_tracks_pacific_dst_boundary(self):
        pools = {
            "serpapi": PoolLimit(2, 5, 2, 5, 1),
            "youtube": PoolLimit(6, 200, 6, 200, 1),
            YOUTUBE_OTHER_POOL: PoolLimit(6, 200, 6, 200, 1),
        }
        control = coordinator(self.path, policy(pools=pools))
        before_reset = datetime(2026, 3, 9, 6, 59, tzinfo=timezone.utc)
        job = control.admit("youtube-before-dst-reset", now=before_reset)["job_id"]
        fence = control.claim(job, "worker", now=before_reset)["fence"]
        control.reserve_attempt(
            job, "worker", fence, source_id=YOUTUBE_SOURCE_ID,
            attempt_id="youtube-dst-before", now=before_reset,
        )
        control.settle_attempt(job, "worker", fence,
                               attempt_id="youtube-dst-before", now=before_reset)
        before_snapshot = control.quota_snapshot("youtube", now=before_reset)
        self.assertEqual(before_snapshot["daily"]["used"], 1)
        self.assertEqual(before_snapshot["daily"]["reset_at"], "2026-03-09T07:00:00+00:00")

        after_reset = datetime(2026, 3, 9, 7, 0, tzinfo=timezone.utc)
        job = control.admit("youtube-after-dst-reset", now=after_reset)["job_id"]
        fence = control.claim(job, "worker", now=after_reset)["fence"]
        control.reserve_attempt(
            job, "worker", fence, source_id=YOUTUBE_SOURCE_ID,
            attempt_id="youtube-dst-after", now=after_reset,
        )
        after_snapshot = control.quota_snapshot("youtube", now=after_reset)
        self.assertEqual(after_snapshot["daily"]["used"], 1)
        self.assertEqual(after_snapshot["daily"]["reset_at"], "2026-03-10T07:00:00+00:00")

    def test_youtube_monthly_usage_history_survives_later_months(self):
        pools = {
            "serpapi": PoolLimit(2, 5, 2, 5, 1),
            "youtube": PoolLimit(6, 200, 6, 200, 1),
            YOUTUBE_OTHER_POOL: PoolLimit(6, 200, 6, 200, 1),
        }
        control = coordinator(self.path, policy(pools=pools))
        november = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        job = control.admit("youtube-history-november", now=november)["job_id"]
        fence = control.claim(job, "worker", now=november)["fence"]
        control.reserve_attempt(
            job, "worker", fence, source_id=YOUTUBE_SOURCE_ID,
            attempt_id="youtube-history-november-attempt", now=november,
        )
        control.settle_attempt(job, "worker", fence,
                               attempt_id="youtube-history-november-attempt", now=november)

        december = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
        job = control.admit("youtube-history-december", now=december)["job_id"]
        fence = control.claim(job, "worker", now=december)["fence"]
        control.reserve_attempt(
            job, "worker", fence, source_id=YOUTUBE_SOURCE_ID,
            attempt_id="youtube-history-december-attempt", now=december,
        )
        quota = control.store.transact("public", lambda state: (state, state["quota"]))
        self.assertEqual(quota["youtube:month:2026-11"], {"requests": 1, "cost": 1})
        self.assertEqual(quota["youtube:month:2026-12"], {"requests": 1, "cost": 1})


if __name__ == "__main__":
    unittest.main()
