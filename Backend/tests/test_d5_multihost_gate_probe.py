"""Offline checks for the explicit opt-in two-host gate harness."""

import asyncio
import hashlib
import multiprocessing
import sys
import tempfile
import types
import unittest
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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


def crash_first_job_process(args):
    path, run_id, host_id, fingerprint, now = args
    store = LocalSQLiteAtomicStateStore(
        path, environment_profile="test", data_mode="SYNTHETIC"
    )
    _, worker = _worker(store, run_id, host_id, fingerprint,
                        crash_first_job=True, utcnow=lambda: now)
    asyncio.run(worker.run_once(host_id))


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

    def test_process_crash_after_claim_is_recovered_by_second_worker(self):
        run_id = "c3d4e5f6"
        now = datetime.now(timezone.utc)
        admission = multihost_gate_probe._admission(self.store, run_id)
        job = admission.admit("crash-recovery-probe-key", now=now)
        context = multiprocessing.get_context("spawn")
        crashed = context.Process(
            target=crash_first_job_process,
            args=((str(self.store.path), run_id, "worker-a", "1" * 64, now),),
        )
        crashed.start()
        crashed.join(timeout=10)
        if crashed.is_alive():
            crashed.terminate()
            crashed.join()
            self.fail("crash-injected worker did not terminate")
        self.assertEqual(crashed.exitcode, 86)

        recovered_at = now + timedelta(seconds=31)
        _, recovery_worker = _worker(
            self.store, run_id, "worker-b", "2" * 64,
            crash_first_job=True, utcnow=lambda: recovered_at,
        )
        result = asyncio.run(recovery_worker.run_once("worker-b"))
        self.assertEqual(result["state"], "COMPLETED")
        saved = admission.read_job(job["job_id"], now=recovered_at)
        source = saved["source_checkpoints"][WIKIMEDIA_SOURCE_ID]["snapshot"]["result"]
        self.assertEqual(saved["attempts"], 2)
        self.assertEqual(source["data_mode"], "SYNTHETIC")
        self.assertEqual(source["records"][0]["worker_host_id"], "worker-b")

    def test_register_only_host_does_not_start_a_worker_runner(self):
        args = SimpleNamespace(
            host_id="worker-b", expected_hosts="worker-a,worker-b",
            run_id="d4e5f6a7", register_only=True,
            idle_poll_seconds=0.5, crash_first_job=False,
        )
        with (
            patch.object(multihost_gate_probe, "_machine_fingerprint",
                         return_value="2" * 64),
            patch.object(multihost_gate_probe, "_wait_for_peers") as wait_for_peers,
            patch.object(multihost_gate_probe, "DistributedWorkerRunner") as runner,
        ):
            result = asyncio.run(multihost_gate_probe._run_worker(args, self.store))

        self.assertEqual(result, 0)
        wait_for_peers.assert_called_once()
        runner.assert_not_called()

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

    def test_cloud_harness_rejects_production_project_id_and_number_before_store(self):
        with patch.object(multihost_gate_probe, "_store") as build_store:
            for project_id in ("trendscout-192e4", "556422620769"):
                with self.subTest(project_id=project_id), self.assertRaisesRegex(
                    SystemExit, "production project"
                ):
                    multihost_gate_probe.main([
                        "--project-id", project_id,
                        "--confirm-nonproduction-project", project_id,
                        "--acknowledge-nonproduction-writes",
                        "--run-id", "a1b2c3d4", "quota-start",
                    ])
        build_store.assert_not_called()

    def test_cloud_harness_rejects_emulator_override_before_store(self):
        with (patch.dict("os.environ", {"FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080"}),
              patch.object(multihost_gate_probe, "_store") as build_store,
              self.assertRaisesRegex(SystemExit, "Emulator override")):
            multihost_gate_probe.main([
                "--project-id", "approved-stage",
                "--confirm-nonproduction-project", "approved-stage",
                "--acknowledge-nonproduction-writes",
                "--run-id", "a1b2c3d4", "quota-start",
            ])
        build_store.assert_not_called()

    def test_windows_machine_fingerprint_hashes_registry_machine_guid(self):
        class RegistryKey:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def open_key(root, path):
            self.assertEqual(root, "HKLM")
            self.assertEqual(path, r"SOFTWARE\Microsoft\Cryptography")
            return RegistryKey()

        def query_value(key, name):
            self.assertEqual(name, "MachineGuid")
            return "synthetic-machine-guid", 1

        fake_winreg = types.SimpleNamespace(
            HKEY_LOCAL_MACHINE="HKLM",
            OpenKey=open_key,
            QueryValueEx=query_value,
        )
        expected = hashlib.sha256(b"synthetic-machine-guid").hexdigest()
        with (patch.object(sys, "platform", "win32"),
              patch.dict(sys.modules, {"winreg": fake_winreg})):
            self.assertEqual(multihost_gate_probe._machine_fingerprint(), expected)

    def test_current_machine_fingerprint_is_hashed_and_usable(self):
        fingerprint = multihost_gate_probe._machine_fingerprint()
        self.assertRegex(fingerprint, r"^[a-f0-9]{64}$")

    def test_windows_worker_shutdown_uses_signal_module_not_asyncio_signal_api(self):
        class FakeLoop:
            def __init__(self):
                self.callbacks = []

            def call_soon_threadsafe(self, callback):
                self.callbacks.append(callback)

            def add_signal_handler(self, *_args):
                raise AssertionError("Windows does not support loop signal handlers")

        class FakeStop:
            def __init__(self):
                self.called = False

            def set(self):
                self.called = True

        loop = FakeLoop()
        stop = FakeStop()
        with (patch.object(sys, "platform", "win32"),
              patch.object(multihost_gate_probe.signal, "signal") as install_signal):
            multihost_gate_probe._install_worker_signal_handlers(loop, stop)

        self.assertEqual(install_signal.call_count, 2)
        handler = install_signal.call_args_list[0].args[1]
        handler(None, None)
        self.assertEqual(len(loop.callbacks), 1)
        loop.callbacks[0]()
        self.assertTrue(stop.called)


if __name__ == "__main__":
    unittest.main()
