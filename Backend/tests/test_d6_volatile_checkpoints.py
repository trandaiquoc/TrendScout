"""Offline tests for process-local provider data and persistent quota state."""

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_volatile_checkpoints import LocalVolatileCheckpoints
from local_security_helpers import private_test_directory


class VolatileCheckpointTests(unittest.TestCase):
    def test_background_sweep_removes_expired_checkpoint_without_access(self):
        current = [datetime.now(timezone.utc)]
        with tempfile.TemporaryDirectory() as directory:
            private = private_test_directory(directory)
            store = LocalSQLiteAtomicStateStore.for_local_live(private / "ledger.sqlite")
            volatile = LocalVolatileCheckpoints(store, clock=lambda: current[0])
            try:
                def write(state):
                    state["jobs"] = {"job": {"source_checkpoints": {"youtube": {
                        "retain_until": (current[0] + timedelta(seconds=1)).isoformat()}}}}
                    return state, None
                volatile.transact("demo", write)
                current[0] += timedelta(seconds=2)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with volatile._mutex:
                        if not volatile._values:
                            break
                    time.sleep(0.05)
                with volatile._mutex:
                    self.assertEqual(volatile._values, {})
            finally:
                volatile.close()

    def test_purge_source_clears_only_volatile_data_and_keeps_quota(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            private = private_test_directory(directory)
            store = LocalSQLiteAtomicStateStore.for_local_live(private / "ledger.sqlite")
            volatile = LocalVolatileCheckpoints(store, clock=lambda: now)
            try:
                def write(state):
                    state["jobs"] = {"job": {"source_checkpoints": {
                        source: {"retain_until": (now + timedelta(seconds=30)).isoformat(),
                                 "snapshot": {"value": "fixture-" + source}}
                        for source in ("youtube", "wikimedia")}}}
                    state["quota"] = {"reserved": 1}
                    return state, None
                volatile.transact("demo", write)
                self.assertEqual(volatile.purge_source("youtube"), 1)
                self.assertEqual(volatile.purge_source("youtube"), 0)
                state = volatile.transact("demo", lambda s: (s, s["jobs"]["job"]["source_checkpoints"].copy()))
                self.assertEqual(set(state), {"wikimedia"})
                self.assertEqual(volatile.transact("demo", lambda s: (s, s["quota"])), {"reserved": 1})
                for file in private.iterdir():
                    if file.is_file() and not file.name.endswith(".process-lock"):
                        self.assertNotIn(b"fixture-youtube", file.read_bytes())
            finally:
                volatile.close()
            with self.assertRaises(ValueError):
                volatile.purge_source("youtube")

    def test_checkpoint_never_reaches_sqlite_and_is_lost_on_restart(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = private_test_directory(directory) / "ledger.sqlite"
            store = LocalSQLiteAtomicStateStore.for_local_live(path)
            volatile = LocalVolatileCheckpoints(store, clock=lambda: now)
            try:
                def write(state):
                    state["jobs"] = {"job": {"source_checkpoints": {"source": {
                        "retain_until": (now + timedelta(seconds=30)).isoformat(),
                        "snapshot": {"value": "fixture-private-checkpoint"}}}}}
                    state["quota"] = {"reserved": 1}
                    return state, None
                volatile.transact("demo", write)
                def read(state):
                    return state, state["jobs"]["job"].get("source_checkpoints", {})
                self.assertIn("source", volatile.transact("demo", read))
                with self.assertRaises(BlockingIOError):
                    LocalVolatileCheckpoints(store, clock=lambda: now)
                for file in path.parent.iterdir():
                    if file.is_file() and not file.name.endswith(".process-lock"):
                        self.assertNotIn(b"fixture-private-checkpoint", file.read_bytes())
            finally:
                volatile.close()
            reopened = LocalVolatileCheckpoints(store, clock=lambda: now)
            try:
                self.assertEqual(reopened.transact("demo", read), {})
                self.assertEqual(reopened.transact("demo", lambda state: (state, state["quota"])),
                                 {"reserved": 1})
            finally:
                reopened.close()

    def test_expired_checkpoint_is_removed_from_memory(self):
        current = [datetime.now(timezone.utc)]
        with tempfile.TemporaryDirectory() as directory:
            private = private_test_directory(directory)
            store = LocalSQLiteAtomicStateStore.for_local_live(private / "ledger.sqlite")
            volatile = LocalVolatileCheckpoints(store, clock=lambda: current[0])
            try:
                def write(state):
                    state["jobs"] = {"job": {"source_checkpoints": {"source": {
                        "retain_until": (current[0] + timedelta(seconds=1)).isoformat()}}}}
                    return state, None
                volatile.transact("demo", write)
                current[0] += timedelta(seconds=2)
                self.assertEqual(volatile.transact("demo", lambda state: (state,
                    state["jobs"]["job"].get("source_checkpoints", {}))), {})
            finally:
                volatile.close()


if __name__ == "__main__":
    unittest.main()
