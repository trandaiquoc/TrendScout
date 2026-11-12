"""Offline checks for the explicit opt-in two-host gate harness."""

import asyncio
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.jobs.multihost_probe import register_host
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, WIKIMEDIA_SOURCE_ID
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from scripts import multihost_gate_probe
from scripts.multihost_gate_probe import (
    _worker,
    inspect_quota_probe,
    prepare_quota_probe,
    reserve_quota_probe,
)


def reserve_quota_from_process(args):
    path, run_id, job_id, fence, host_id, fingerprint, now = args
    store = LocalSQLiteAtomicStateStore(
        path, environment_profile="test", data_mode="SYNTHETIC"
    )
    return reserve_quota_probe(
        store, run_id=run_id, job_id=job_id, fence=fence, host_id=host_id,
        machine_fingerprint=fingerprint, expected_hosts=("worker-a", "worker-b"),
        timeout_seconds=5, now=now,
    )


class MultiHostProbeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = LocalSQLiteAtomicStateStore(
            str(Path(self.directory.name) / "probe.sqlite"),
            environment_profile="local", data_mode="SYNTHETIC",
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_records_two_distinct_hosts_and_rejects_one_machine_as_two_hosts(self):
        expected = ("worker-a", "worker-b")
        count = register_host(self.store, run_id="a1b2c3d4", host_id="worker-a",
                              machine_fingerprint="1" * 64, expected_hosts=expected)
        self.assertEqual(count, 1)
        with self.assertRaisesRegex(ValueError, "same machine"):
            register_host(self.store, run_id="a1b2c3d4", host_id="worker-b",
                          machine_fingerprint="1" * 64,
                          expected_hosts=expected)
        with self.assertRaisesRegex(ValueError, "changed machine identity"):
            register_host(self.store, run_id="a1b2c3d4", host_id="worker-a",
                          machine_fingerprint="3" * 64, expected_hosts=expected)

    def test_rejects_run_scope_mismatch(self):
        register_host(self.store, run_id="a1b2c3d4", host_id="worker-a",
                      machine_fingerprint="1" * 64,
                      expected_hosts=("worker-a", "worker-b"))
        with self.assertRaisesRegex(ValueError, "scope mismatch"):
            register_host(self.store, run_id="a1b2c3d4", host_id="worker-a",
                          machine_fingerprint="2" * 64,
                          expected_hosts=("worker-a", "worker-c"))

    def test_rejects_unexpected_registered_host_and_invalid_expected_id(self):
        self.store.transact("d5mh-host-a1b2c3d4", lambda _state: ({
            "schema": 1, "run_id": "a1b2c3d4",
            "expected_hosts": ["worker-a", "worker-b"],
            "hosts": {"rogue": {"machine_fingerprint": "3" * 64}},
        }, None))
        with self.assertRaisesRegex(ValueError, "unexpected host"):
            register_host(self.store, run_id="a1b2c3d4", host_id="worker-a",
                          machine_fingerprint="1" * 64,
                          expected_hosts=("worker-a", "worker-b"))
        with self.assertRaisesRegex(ValueError, "invalid multi-host registration"):
            register_host(self.store, run_id="b2c3d4e5", host_id="worker-a",
                          machine_fingerprint="1" * 64,
                          expected_hosts=("worker-a", "bad/id"))

    def test_probe_worker_completes_only_synthetic_job(self):
        admission, worker = _worker(self.store, "a1b2c3d4", "worker-a", "1" * 64)
        job = admission.admit("multi-host-probe-test-key", now=datetime.now(timezone.utc))
        result = asyncio.run(worker.run_once("worker-a"))
        self.assertEqual(result["state"], "COMPLETED")
        saved = admission.read_job(job["job_id"], now=datetime.now(timezone.utc))
        stored = saved["source_checkpoints"][WIKIMEDIA_SOURCE_ID]["snapshot"]["result"]
        self.assertEqual(stored["data_mode"], "SYNTHETIC")
        self.assertEqual(stored["records"][0]["fixture"], "D5_MULTIHOST_PROBE")
        self.assertEqual(stored["records"][0]["worker_host_id"], "worker-a")

    def test_two_independent_processes_race_one_shared_synthetic_quota_unit(self):
        run_id = "b2c3d4e5"
        now = datetime.now(timezone.utc)
        probe = prepare_quota_probe(self.store, run_id, now=now)
        self.assertEqual(probe["provider_calls"], 0)
        self.assertEqual(probe["quota_limit"], 1)
        context = multiprocessing.get_context("spawn")
        args = [
            (str(self.store.path), run_id, probe["job_id"], probe["fence"], "worker-a",
             "1" * 64, now),
            (str(self.store.path), run_id, probe["job_id"], probe["fence"], "worker-b",
             "2" * 64, now),
        ]
        with ProcessPoolExecutor(max_workers=2, mp_context=context) as processes:
            results = list(processes.map(reserve_quota_from_process, args))
        self.assertEqual(sorted(result["result"] for result in results),
                         ["NEW", "QUOTA_EXHAUSTED"])
        self.assertEqual({result["source_id"] for result in results},
                         {GOOGLE_SOURCE_ID, BING_SOURCE_ID})
        self.assertTrue(all(result["provider_calls"] == 0 for result in results))
        summary = inspect_quota_probe(self.store, run_id, probe["job_id"])
        self.assertEqual(summary["daily_reservations"], 1)
        self.assertEqual(summary["monthly_reservations"], 1)
        self.assertTrue(summary["quota_race_pass"])
        self.assertEqual(sorted(value["result"] for value in summary["host_results"].values()),
                         ["NEW", "QUOTA_EXHAUSTED"])

    def test_cloud_harness_requires_explicit_opt_in_before_store_construction(self):
        with patch.object(multihost_gate_probe, "_store") as build_store:
            for action in ("submit", "quota-start"):
                with self.subTest(action=action), self.assertRaises(SystemExit):
                    argv = [
                        "--project-id", "some-project", "--run-id", "a1b2c3d4",
                        "--confirm-nonproduction-project", "some-project", action,
                    ]
                    if action == "submit":
                        argv.extend(("--idempotency-key", "multi-host-probe-key"))
                    multihost_gate_probe.main(argv)
        build_store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
