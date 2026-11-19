"""Collections Firestore snapshots are bounded, versioned and offline-testable."""

import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.collections.firestore_snapshot import (
    CollectionSnapshotConflict,
    FirestoreCollectionSnapshot,
)
from app.storage import firestore_atomic_state, firestore_snapshot_blobs


class FakeFirestoreClient:
    def collection(self, _name):
        return object()

    def transaction(self, **_kwargs):
        return object()


class FakeStateStore:
    def __init__(self):
        self.values = {}

    def transact(self, key, operation):
        current = copy.deepcopy(self.values.get(key, {}))
        updated, result = operation(current)
        if updated != current:
            self.values[key] = copy.deepcopy(updated)
        return result


class FakeSnapshotBlobs:
    def __init__(self):
        self.records = {}
        self.writes = 0

    def put(self, records, *, now, retain_until):
        self.writes += 1
        digest = str(self.writes)
        self.records[digest] = copy.deepcopy(records)
        return {"digest": digest, "item_count": len(records), "raw_bytes": 1}

    def get(self, reference, *, now):
        return copy.deepcopy(self.records[reference["digest"]])


class FirestoreCollectionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.state = FakeStateStore()
        self.blobs = FakeSnapshotBlobs()
        self.patches = [
            patch.object(firestore_atomic_state, "FirestoreAtomicStateStore",
                  return_value=self.state),
            patch.object(firestore_snapshot_blobs, "FirestoreSnapshotBlobs",
                  return_value=self.blobs),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def _snapshot(self, path):
        return FirestoreCollectionSnapshot(
            FakeFirestoreClient(), path,
            utcnow=lambda: datetime(2026, 11, 19, tzinfo=timezone.utc),
        )

    def test_round_trip_and_unchanged_image_does_not_write_again(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite"
            with closing(sqlite3.connect(source)) as db:
                db.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT)")
                db.execute("INSERT INTO collections VALUES ('c1', 'Demo')")
                db.commit()

            writer = self._snapshot(source)
            writer.load()
            self.assertEqual(writer.save(), 1)
            self.assertEqual(self.blobs.writes, 1)
            self.assertEqual(writer.save(), 1)
            self.assertEqual(self.blobs.writes, 1)

            restored_path = Path(directory) / "restored.sqlite"
            restored = self._snapshot(restored_path)
            self.assertEqual(restored.load(), 1)
            with closing(sqlite3.connect(restored_path)) as db:
                self.assertEqual(db.execute("SELECT id, name FROM collections").fetchall(),
                                 [("c1", "Demo")])

            with closing(sqlite3.connect(restored_path)) as db:
                db.execute("UPDATE collections SET name='Updated' WHERE id='c1'")
                db.commit()
            self.assertEqual(restored.save(), 2)
            self.assertEqual(self.blobs.writes, 2)

    def test_stale_writer_cannot_replace_newer_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collections.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT)")
                db.execute("INSERT INTO collections VALUES ('c1', 'Demo')")
                db.commit()
            first = self._snapshot(path)
            first.load()
            first.save()

            second_path = Path(directory) / "second.sqlite"
            second = self._snapshot(second_path)
            second.load()
            with closing(sqlite3.connect(second_path)) as db:
                db.execute("UPDATE collections SET name='Stale' WHERE id='c1'")
                db.commit()

            pointer = self.state.values["collections-live-snapshot-v1"]
            self.state.values["collections-live-snapshot-v1"] = {
                **pointer, "version": pointer["version"] + 1,
            }
            with self.assertRaises(CollectionSnapshotConflict):
                second.save()


if __name__ == "__main__":
    unittest.main()
