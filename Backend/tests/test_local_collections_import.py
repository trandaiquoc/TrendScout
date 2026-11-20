"""Offline safety checks for the prepared one-time SQLite merge utility."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.collections.store import CollectionStore
from scripts.import_local_collections_to_firestore import _make_backup, merge_local_database


class LocalCollectionsImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.source_path = root / "local.sqlite"
        self.target_path = root / "firestore-working.sqlite"
        self.source = CollectionStore(self.source_path, data_mode_scope="LIVE")
        self.target = CollectionStore(self.target_path, data_mode_scope="LIVE")
        self._insert_fixture(self.source, description="local description", evidence="local evidence")

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def _insert_fixture(store, *, description, evidence, membership=True):
        with store.connect() as database:
            database.execute(
                "INSERT OR IGNORE INTO collections(id,name,description,created_by,created_at,updated_at) "
                "VALUES('local-topic','Local topic',?,'USER','2026-11-20T00:00:00+00:00',"
                "'2026-11-20T00:00:00+00:00')",
                (description,),
            )
            database.execute(
                "INSERT OR IGNORE INTO keywords(id,keyword,normalized,source,project,observed_at,"
                "data_mode,evidence) VALUES('local-keyword','Fixture','fixture',"
                "'wikimedia_pageviews_top_per_country','en.wikipedia','2026-11-20', 'LIVE', ?)",
                (evidence,),
            )
            if membership:
                database.execute(
                    "INSERT OR IGNORE INTO memberships(collection_id,keyword_id,origin,created_at) "
                    "VALUES('local-topic','local-keyword','USER','2026-11-20T00:00:00+00:00')"
                )

    def test_merge_is_idempotent_and_does_not_replace_existing_rows(self):
        self._insert_fixture(self.target, description="existing description",
                             evidence="existing evidence", membership=False)

        first = merge_local_database(self.source_path, self.target_path)
        second = merge_local_database(self.source_path, self.target_path)

        self.assertEqual(first["collections"], 0)
        self.assertEqual(first["keywords"], 0)
        self.assertEqual(first["memberships"], 1)
        self.assertEqual(second["memberships"], 0)
        with self.target.connect() as database:
            self.assertEqual(database.execute(
                "SELECT description FROM collections WHERE id='local-topic'"
            ).fetchone()[0], "existing description")
            self.assertEqual(database.execute(
                "SELECT evidence FROM keywords WHERE id='local-keyword'"
            ).fetchone()[0], "existing evidence")

    def test_conflicting_collection_identity_aborts_before_inserting_rows(self):
        self.target_path.unlink()
        self.target = CollectionStore(self.target_path, data_mode_scope="LIVE")
        with self.target.connect() as database:
            database.execute(
                "INSERT INTO collections(id,name,description,created_by,created_at,updated_at) "
                "VALUES('local-topic','Different topic','target','USER',"
                "'2026-11-20T00:00:00+00:00','2026-11-20T00:00:00+00:00')"
            )

        with self.assertRaisesRegex(ValueError, "collections identity conflicts"):
            merge_local_database(self.source_path, self.target_path)
        with self.target.connect() as database:
            self.assertEqual(database.execute("SELECT count(*) FROM keywords").fetchone()[0], 0)

    def test_snapshot_backup_refuses_to_overwrite_an_existing_backup(self):
        backup_directory = Path(self.directory.name) / "backups"
        fixed = datetime(2026, 11, 20, tzinfo=timezone.utc)
        original = self.target_path.read_bytes()

        class FixedClock:
            @staticmethod
            def now(_timezone):
                return fixed

        with patch("scripts.import_local_collections_to_firestore.datetime", FixedClock):
            backup = _make_backup(backup_directory, self.target_path)
            self.target_path.write_bytes(b"changed target")
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                _make_backup(backup_directory, self.target_path)

        self.assertEqual(backup.read_bytes(), original)

    def test_import_rejects_non_live_source_rows(self):
        with self.source.connect() as database:
            database.execute(
                "UPDATE keywords SET data_mode='SYNTHETIC' WHERE id='local-keyword'"
            )

        with self.assertRaisesRegex(ValueError, "non-LIVE"):
            merge_local_database(self.source_path, self.target_path)
        with self.target.connect() as database:
            self.assertEqual(database.execute("SELECT count(*) FROM keywords").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
