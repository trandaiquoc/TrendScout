"""Offline checks for the explicit opt-in two-host gate harness."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.jobs.multihost_probe import register_host
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from scripts import multihost_gate_probe
from scripts.multihost_gate_probe import _worker


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

    def test_cloud_harness_requires_explicit_opt_in_before_store_construction(self):
        with patch.object(multihost_gate_probe, "_store") as build_store:
            with self.assertRaises(SystemExit):
                multihost_gate_probe.main([
                    "--project-id", "some-project", "--run-id", "a1b2c3d4",
                    "--confirm-nonproduction-project", "some-project",
                    "submit", "--idempotency-key", "multi-host-probe-key",
                ])
        build_store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
