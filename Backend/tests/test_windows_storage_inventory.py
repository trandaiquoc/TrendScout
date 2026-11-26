import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.storage.local_file_security import secure_private_directory
from scripts.inventory_windows_storage import SQLiteSource, capture_database, default_sources


class WindowsStorageInventoryTests(unittest.TestCase):
    def test_default_sources_cover_business_and_operational_sqlite_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = default_sources(
                local_app_data=root / "AppData" / "Local",
                home=root / "Home",
                environment={"TRENDSCOUT_COLLECTIONS_DIR": str(root / "custom-collections")},
            )
        by_id = {source.source_id: source for source in sources}
        self.assertEqual(
            set(by_id),
            {"collections", "scheduler", "normal_google_live",
             "normal_wikimedia_live", "d6_smoke"},
        )
        self.assertEqual(by_id["collections"].path.name, "collections.sqlite")
        self.assertEqual(by_id["collections"].location_label,
                         "configured collections dir/collections.sqlite")
        self.assertEqual(by_id["scheduler"].path.name, "schedules.sqlite")
        self.assertEqual(by_id["normal_google_live"].path.name, "state.sqlite")
        self.assertEqual(by_id["normal_wikimedia_live"].path.name, "state.sqlite")
        self.assertEqual(by_id["d6_smoke"].path.name, "ledger.sqlite")

    def test_capture_includes_committed_wal_rows_without_exporting_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_directory = root / "source"
            output_directory = root / "output"
            secure_private_directory(source_directory)
            secure_private_directory(output_directory)
            database = source_directory / "collections.sqlite"
            source_db = sqlite3.connect(database)
            try:
                source_db.execute("PRAGMA journal_mode=WAL")
                source_db.execute("CREATE TABLE user_data (id TEXT PRIMARY KEY, note TEXT)")
                source_db.execute(
                    "INSERT INTO user_data VALUES (?, ?)",
                    ("row-1", "private-value-must-not-appear-in-inventory"),
                )
                source_db.commit()
                if os.name != "nt":
                    database.chmod(0o600)
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(database) + suffix)
                        if sidecar.exists():
                            sidecar.chmod(0o600)
                result = capture_database(
                    SQLiteSource("collections", database, "collections-v1/collections.sqlite"),
                    output_directory,
                )
            finally:
                source_db.close()

            self.assertEqual(result["status"], "captured")
            self.assertEqual(result["integrity_check"], "ok")
            self.assertEqual(result["foreign_key_violation_count"], 0)
            self.assertGreater(result["source_wal_sidecar_bytes"], 0)
            self.assertEqual(result["tables"][0]["row_count"], 1)
            self.assertEqual(len(result["backup_sha256"]), 64)
            self.assertNotIn("private-value-must-not-appear-in-inventory", json.dumps(result))

            backup = output_directory / result["backup_file"]
            with closing(sqlite3.connect(
                backup.absolute().as_uri() + "?mode=ro&immutable=1", uri=True
            )) as backup_db:
                self.assertEqual(backup_db.execute("SELECT count(*) FROM user_data").fetchone()[0], 1)

    def test_missing_database_is_not_created_and_orphan_wal_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_directory = root / "output"
            secure_private_directory(output_directory)
            missing = root / "missing.sqlite"
            result = capture_database(
                SQLiteSource("missing", missing, "missing.sqlite"), output_directory
            )
            self.assertEqual(result["status"], "missing")
            self.assertFalse(missing.exists())

            orphan = root / "orphan.sqlite"
            Path(str(orphan) + "-wal").write_bytes(b"unresolved")
            failed = capture_database(
                SQLiteSource("orphan", orphan, "orphan.sqlite"), output_directory
            )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error_type"], "OrphanSQLiteSidecar")
            self.assertTrue(Path(str(orphan) + "-wal").exists())


if __name__ == "__main__":
    unittest.main()
