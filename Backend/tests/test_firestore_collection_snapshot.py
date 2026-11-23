"""Collections Firestore snapshots are bounded, versioned and offline-testable."""

import copy
import json
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.collections import firestore_snapshot
from app.collections.firestore_snapshot import (
    CollectionSnapshotConflict,
    CollectionSnapshotUnavailable,
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
        self.read_many_calls = 0
        self.transact_calls = 0

    def transact(self, key, operation):
        self.transact_calls += 1
        current = copy.deepcopy(self.values.get(key, {}))
        updated, result = operation(current)
        if updated != current:
            self.values[key] = copy.deepcopy(updated)
        return result

    def read_many(self, keys):
        self.read_many_calls += 1
        return {key: copy.deepcopy(self.values.get(key, {})) for key in keys}


class FakeSnapshotBlobs:
    def __init__(self):
        self.records = {}
        self.writes = 0
        self.reads = 0

    def put(self, records, *, now, retain_until):
        self.writes += 1
        digest = str(self.writes)
        self.records[digest] = copy.deepcopy(records)
        return {"digest": digest, "item_count": len(records), "raw_bytes": 1}

    def get(self, reference, *, now):
        self.reads += 1
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

    def test_compressed_limit_fits_existing_firestore_blob_budget(self):
        envelope = json.dumps(
            [{"encoding": "sqlite-gzip-base64-v1", "payload": ""}],
            separators=(",", ":"),
        ).encode("utf-8")
        payload_chars = 4 * ((firestore_snapshot._MAX_COMPRESSED_BYTES + 2) // 3)
        encoded_record_size = len(envelope) + payload_chars
        self.assertLessEqual(encoded_record_size, firestore_snapshot_blobs._MAX_BLOB_BYTES)

    def test_load_reads_pointer_without_opening_a_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self._snapshot(Path(directory) / "restored.sqlite")
            self.assertEqual(snapshot.load(), 0)

        self.assertEqual(self.state.read_many_calls, 1)
        self.assertEqual(self.state.transact_calls, 0)

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
            with patch.object(
                FirestoreCollectionSnapshot, "_content_digest",
                side_effect=AssertionError("restore must reuse the stored digest"),
            ):
                self.assertEqual(restored.load(), 1)
            with closing(sqlite3.connect(restored_path)) as db:
                self.assertEqual(db.execute("SELECT id, name FROM collections").fetchall(),
                                 [("c1", "Demo")])

            with closing(sqlite3.connect(restored_path)) as db:
                db.execute("UPDATE collections SET name='Updated' WHERE id='c1'")
                db.commit()
            self.assertEqual(restored.save(), 2)
            self.assertEqual(self.blobs.writes, 2)

    def test_latest_dataset_summary_reads_without_restoring_sqlite_blob(self):
        summary = {"dataset_id": "fetch-1", "status": "COMPLETED", "sources": ["google"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collections.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE collections (id TEXT PRIMARY KEY)")
                db.execute("INSERT INTO collections VALUES ('c1')")
                db.commit()
            writer = self._snapshot(path)
            writer.load()
            writer.set_latest_dataset_summary_provider(lambda: summary)
            self.assertEqual(writer.save(), 1)

            reader = self._snapshot(Path(directory) / "not-restored.sqlite")
            available, cached_summary = reader.read_latest_dataset_summary()

        self.assertTrue(available)
        self.assertEqual(cached_summary, summary)
        self.assertEqual(self.blobs.reads, 0)
        self.assertEqual(self.state.values["collections-live-snapshot-v1"][
            "latest_dataset_summary_version"], 1)

    def test_legacy_summary_fallback_reuses_pointer_read_for_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite"
            with closing(sqlite3.connect(source)) as db:
                db.execute("CREATE TABLE collections (id TEXT PRIMARY KEY)")
                db.execute("INSERT INTO collections VALUES ('c1')")
                db.commit()

            writer = self._snapshot(source)
            writer.load()
            writer.save()
            self.state.read_many_calls = 0

            restored = self._snapshot(Path(directory) / "restored.sqlite")
            available, summary = restored.read_latest_dataset_summary()
            self.assertFalse(available)
            self.assertIsNone(summary)
            self.assertEqual(restored.load(), 1)

        self.assertEqual(self.state.read_many_calls, 1)
        self.assertEqual(self.blobs.reads, 1)

    def test_unchanged_snapshot_backfills_legacy_summary_without_reupload(self):
        summary = {"dataset_id": "fetch-legacy", "status": "PARTIAL", "sources": []}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite"
            with closing(sqlite3.connect(source)) as db:
                db.execute("CREATE TABLE collections (id TEXT PRIMARY KEY)")
                db.execute("INSERT INTO collections VALUES ('c1')")
                db.commit()
            writer = self._snapshot(source)
            writer.load()
            writer.save()
            pointer = self.state.values["collections-live-snapshot-v1"]
            pointer.pop("latest_dataset_summary_version", None)
            pointer.pop("latest_dataset_summary", None)

            restored = self._snapshot(Path(directory) / "restored.sqlite")
            restored.load()
            restored.set_latest_dataset_summary_provider(lambda: summary)
            self.assertEqual(restored.save(), 1)

        self.assertEqual(self.blobs.writes, 1)
        available, cached_summary = self._snapshot(Path(directory) / "unused.sqlite").read_latest_dataset_summary()
        self.assertTrue(available)
        self.assertEqual(cached_summary, summary)

    def test_missing_pointer_is_a_readable_empty_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = self._snapshot(Path(directory) / "empty.sqlite")
            available, summary = reader.read_latest_dataset_summary()

        self.assertTrue(available)
        self.assertIsNone(summary)
        self.assertEqual(self.blobs.reads, 0)

    def test_legacy_pointer_without_content_digest_is_still_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite"
            with closing(sqlite3.connect(source)) as db:
                db.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT)")
                db.execute("INSERT INTO collections VALUES ('c1', 'Demo')")
                db.commit()

            writer = self._snapshot(source)
            writer.load()
            writer.save()
            pointer = self.state.values["collections-live-snapshot-v1"]
            pointer.pop("sqlite_content_sha256")

            restored = self._snapshot(Path(directory) / "restored.sqlite")
            with patch.object(
                FirestoreCollectionSnapshot, "_content_digest",
                wraps=FirestoreCollectionSnapshot._content_digest,
            ) as digest:
                self.assertEqual(restored.load(), 1)
            digest.assert_called_once_with(restored.path)

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

    def test_upload_failure_keeps_only_safe_failure_diagnostics(self):
        class FailingBlobs(FakeSnapshotBlobs):
            def put(self, *_args, **_kwargs):
                raise RuntimeError("credential-value-must-not-escape")

        self.blobs.put = FailingBlobs().put
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collections.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE collections (id TEXT PRIMARY KEY)")
                db.commit()
            snapshot = self._snapshot(path)
            snapshot.load()

            with self.assertRaises(CollectionSnapshotUnavailable) as captured:
                snapshot.save()

        error = captured.exception
        self.assertEqual(error.failure_reason, "BLOB_UPLOAD_FAILED")
        self.assertEqual(error.cause_class, "RuntimeError")
        self.assertIsNone(error.confirmation_exception_class)
        self.assertIsNone(error.database_size_bytes)
        self.assertIsNone(error.compressed_size_bytes)
        self.assertNotIn("credential-value-must-not-escape", str(error))

    def test_round_trips_database_above_previous_expanded_limit(self):
        payload_size = 22_000_000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collections.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE source_data (id TEXT PRIMARY KEY, payload BLOB NOT NULL)")
                db.execute("INSERT INTO source_data VALUES ('saved',?)", (b"x" * payload_size,))
                db.commit()

            snapshot = self._snapshot(path)
            snapshot.load()
            self.assertEqual(snapshot.save(), 1)
            self.assertEqual(self.blobs.writes, 1)

            restored_path = Path(directory) / "restored.sqlite"
            restored = self._snapshot(restored_path)
            restored.load()
            with closing(sqlite3.connect(restored_path)) as db:
                row = db.execute("SELECT length(payload) FROM source_data WHERE id='saved'").fetchone()
                self.assertEqual(row, (payload_size,))

    def test_oversized_active_data_reports_bounded_size_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collections.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE user_data (payload BLOB NOT NULL)")
                db.execute("INSERT INTO user_data VALUES(?)", (b"x" * (128 * 1024),))
                db.commit()

            with patch("app.collections.firestore_snapshot._MAX_DATABASE_BYTES", 64 * 1024):
                snapshot = self._snapshot(path)
                snapshot.load()
                with self.assertRaises(CollectionSnapshotUnavailable) as captured:
                    snapshot.save()

            self.assertEqual(captured.exception.failure_reason, "DATABASE_TOO_LARGE")
            self.assertGreater(captured.exception.database_size_bytes, 64 * 1024)
            self.assertIsNone(captured.exception.compressed_size_bytes)
            self.assertEqual(self.blobs.writes, 0)

    def test_oversized_snapshot_compacts_freelist_without_mutating_working_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collections.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT)")
                db.execute("CREATE TABLE obsolete_rows (payload BLOB NOT NULL)")
                db.execute("INSERT INTO collections VALUES ('c1', 'Demo')")
                db.executemany("INSERT INTO obsolete_rows VALUES(?)",
                               [(b"x" * 8192,) for _ in range(64)])
                db.commit()
                db.execute("DELETE FROM obsolete_rows")
                db.commit()
            original_size = path.stat().st_size
            self.assertGreater(original_size, 64 * 1024)

            with patch("app.collections.firestore_snapshot._MAX_DATABASE_BYTES", 64 * 1024):
                snapshot = self._snapshot(path)
                snapshot.load()
                self.assertEqual(snapshot.save(), 1)

            self.assertEqual(path.stat().st_size, original_size)
            restored_path = Path(directory) / "restored.sqlite"
            restored = self._snapshot(restored_path)
            restored.load()
            with closing(sqlite3.connect(restored_path)) as db:
                self.assertEqual(db.execute("SELECT name FROM collections WHERE id='c1'").fetchone(),
                                 ("Demo",))
                self.assertEqual(db.execute("SELECT count(*) FROM obsolete_rows").fetchone()[0], 0)

    def test_oversized_snapshot_drops_only_recomputable_embedding_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collections.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT)")
                db.execute("CREATE TABLE cache (fingerprint TEXT PRIMARY KEY, vector BLOB NOT NULL)")
                db.execute("INSERT INTO collections VALUES ('c1', 'Demo')")
                db.executemany("INSERT INTO cache VALUES(?,?)",
                               [(str(index), bytes([index]) * 8192) for index in range(32)])
                db.commit()
            original_size = path.stat().st_size
            self.assertGreater(original_size, 64 * 1024)

            with patch("app.collections.firestore_snapshot._MAX_DATABASE_BYTES", 64 * 1024):
                snapshot = self._snapshot(path)
                snapshot.load()
                self.assertEqual(snapshot.save(), 1)

            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM cache").fetchone()[0], 32)
            restored_path = Path(directory) / "restored.sqlite"
            restored = self._snapshot(restored_path)
            restored.load()
            with closing(sqlite3.connect(restored_path)) as db:
                self.assertEqual(db.execute("SELECT name FROM collections WHERE id='c1'").fetchone(),
                                 ("Demo",))
                self.assertEqual(db.execute("SELECT count(*) FROM cache").fetchone()[0], 0)

    def test_compressed_size_fallback_drops_only_recomputable_embedding_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collections.sqlite"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT)")
                db.execute("CREATE TABLE cache (fingerprint TEXT PRIMARY KEY, vector BLOB NOT NULL)")
                db.execute("INSERT INTO collections VALUES ('c1', 'Demo')")
                db.execute("INSERT INTO cache VALUES ('cached',?)",
                           (random.Random(90210).randbytes(96_000),))
                db.commit()
            self.assertLess(path.stat().st_size, 20_000_000)

            with patch("app.collections.firestore_snapshot._MAX_COMPRESSED_BYTES", 64 * 1024):
                snapshot = self._snapshot(path)
                snapshot.load()
                self.assertEqual(snapshot.save(), 1)

            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM cache").fetchone()[0], 1)
            restored_path = Path(directory) / "restored.sqlite"
            restored = self._snapshot(restored_path)
            restored.load()
            with closing(sqlite3.connect(restored_path)) as db:
                self.assertEqual(db.execute("SELECT name FROM collections WHERE id='c1'").fetchone(),
                                 ("Demo",))
                self.assertEqual(db.execute("SELECT count(*) FROM cache").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
