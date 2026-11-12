"""Offline tests for process-local provider data and persistent quota state."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore
from app.storage.local_volatile_checkpoints import LocalVolatileCheckpoints


class VolatileCheckpointTests(unittest.TestCase):
    def test_checkpoint_never_reaches_sqlite_and_is_lost_on_restart(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite"
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
                for file in Path(directory).iterdir():
                    if file.is_file():
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
            store = LocalSQLiteAtomicStateStore.for_local_live(Path(directory) / "ledger.sqlite")
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
