"""Merge the local LIVE Collections ledger into the existing Firestore snapshot.

Dry-run is the default. --apply uses the repository's approved keyless
impersonated client, keeps a timestamped copy of the existing snapshot, inserts
rows without replacing conflicts, and commits through the existing versioned
Firestore snapshot contract. Embeddings and algorithm result rows are omitted
so the remaining ledger fits the current snapshot size budget.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.collections.firestore_snapshot import FirestoreCollectionSnapshot
from app.collections.store import CollectionStore
from app.orchestration.firestore_shared_live_composition import (
    PRODUCTION_FIRESTORE_PROJECT_ID,
)
from app.storage.local_file_security import secure_private_directory

EXCLUDED_TABLES = frozenset({"algorithm_results", "cache", "settings"})
IMPORT_TABLES = frozenset({
    "algorithm_observations", "classification_results", "collections",
    "dataset_snapshots", "entity_context", "feedback", "fetch_jobs",
    "keyword_decisions", "keywords", "membership_reconciliation_items",
    "membership_reconciliation_runs", "memberships", "proposals",
    "research_records", "suppression",
})
IDENTITY_COLUMNS = {
    "collections": ("id", ("name", "parent_id", "created_by", "category_id")),
    "keywords": ("id", ("keyword", "normalized", "source", "project", "observed_at", "data_mode")),
}


def _quote(identifier: str) -> str:
    if not identifier or any(not (char.isalnum() or char == "_") for char in identifier):
        raise ValueError("unexpected SQLite identifier")
    return f'"{identifier}"'


def _tables(database):
    return {row[0] for row in database.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _columns(database, table):
    return tuple(row[1] for row in database.execute(f"PRAGMA table_info({_quote(table)})"))


def _integrity_check(database, label):
    result = database.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise ValueError(f"{label} SQLite integrity check failed")
    if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError(f"{label} SQLite foreign-key check failed")


def _validate_live_source(database):
    tables = _tables(database)
    if not {"keywords", "collections", "dataset_snapshots"} <= tables:
        raise ValueError("source database is not a TrendScout Collections ledger")
    for table in ("keywords", "dataset_snapshots"):
        columns = _columns(database, table)
        if "data_mode" in columns:
            modes = {row[0] for row in database.execute(
                f"SELECT DISTINCT data_mode FROM {_quote(table)}"
            )}
            if modes - {"LIVE"}:
                raise ValueError("source database contains non-LIVE rows")


def _check_identity_conflicts(source, target, table, key_column, identity_columns):
    selected = (key_column, *identity_columns)
    projection = ",".join(_quote(column) for column in selected)
    target_lookup = target.cursor()
    lookup = (f"SELECT {projection} FROM {_quote(table)} "
              f"WHERE {_quote(key_column)}=?")
    for row in source.execute(f"SELECT {projection} FROM {_quote(table)}"):
        existing = target_lookup.execute(lookup, (row[0],)).fetchone()
        if existing is not None and tuple(existing[1:]) != tuple(row[1:]):
            raise ValueError(f"existing {table} identity conflicts with local data")


def merge_local_database(source_path: Path, target_path: Path) -> dict[str, int]:
    """Insert compatible local rows while preserving every existing target row."""
    source_path, target_path = Path(source_path), Path(target_path)
    if source_path.is_symlink() or target_path.is_symlink():
        raise ValueError("source and target databases must be distinct regular files")
    source_path = source_path.resolve(strict=True)
    target_path = target_path.resolve(strict=True)
    if source_path == target_path:
        raise ValueError("source and target databases must be distinct regular files")

    source_uri = source_path.as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=10)
    target = sqlite3.connect(target_path, timeout=10)
    try:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        target.execute("PRAGMA foreign_keys=ON")
        _integrity_check(source, "source")
        _integrity_check(target, "target")
        _validate_live_source(source)

        source_tables, target_tables = _tables(source), _tables(target)
        unapproved = source_tables - IMPORT_TABLES - EXCLUDED_TABLES
        if unapproved:
            raise ValueError("source database contains tables outside the import allowlist")
        import_tables = sorted(source_tables & IMPORT_TABLES)
        missing = set(import_tables) - target_tables
        if missing:
            raise ValueError("source database contains tables absent from the target schema")
        for table in import_tables:
            if set(_columns(source, table)) != set(_columns(target, table)):
                raise ValueError("source and target SQLite schemas differ")
        for table, (key_column, identity_columns) in IDENTITY_COLUMNS.items():
            _check_identity_conflicts(source, target, table, key_column, identity_columns)

        inserted = {}
        target.execute("BEGIN")
        target.execute("PRAGMA defer_foreign_keys=ON")
        for table in import_tables:
            columns = _columns(target, table)
            quoted_columns = ",".join(_quote(column) for column in columns)
            placeholders = ",".join("?" for _ in columns)
            statement = (f"INSERT OR IGNORE INTO {_quote(table)} ({quoted_columns}) "
                         f"VALUES ({placeholders})")
            before = target.total_changes
            cursor = source.execute(f"SELECT {quoted_columns} FROM {_quote(table)}")
            while rows := cursor.fetchmany(500):
                target.executemany(statement, rows)
            inserted[table] = target.total_changes - before

        if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("merged database would violate a foreign key")
        target.commit()
        _integrity_check(target, "merged")
        for table in sorted(source_tables & EXCLUDED_TABLES):
            inserted[f"omitted_{table}"] = source.execute(
                f"SELECT count(*) FROM {_quote(table)}"
            ).fetchone()[0]
        return inserted
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


def _default_source_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "TrendScout" / "collections-v1" / "collections.sqlite"
    return Path.home() / ".local" / "state" / "trendscout" / "collections-v1" / "collections.sqlite"


def _make_backup(backup_directory: Path, database_path: Path) -> Path | None:
    if not database_path.exists():
        return None
    secure_private_directory(backup_directory)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_directory / f"collections-before-local-import-{timestamp}.sqlite"
    if backup_path.exists() or backup_path.is_symlink():
        raise FileExistsError("snapshot backup already exists; refusing to overwrite it")
    try:
        with database_path.open("rb") as source, backup_path.open("xb") as destination:
            shutil.copyfileobj(source, destination)
    except OSError:
        backup_path.unlink(missing_ok=True)
        raise ValueError("existing Firestore snapshot backup could not be created") from None
    try:
        backup_path.chmod(0o600)
    except OSError:
        backup_path.unlink(missing_ok=True)
        raise ValueError("snapshot backup permissions could not be restricted") from None
    return backup_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=_default_source_path())
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true",
                        help="commit the merge to the existing Firestore snapshot")
    args = parser.parse_args(argv)

    source_path = args.source_db
    if not source_path.is_file() or source_path.is_symlink():
        parser.error("local Collections SQLite database was not found as a regular file")
    backup_directory = args.backup_dir or source_path.parent / "firestore-import-backups"

    try:
        from scripts.run_google_live_local import _create_firestore_shared_live_client

        client = _create_firestore_shared_live_client(refresh_credentials=True)
    except Exception as error:
        parser.exit(2, f"Firestore access unavailable ({type(error).__name__}); "
                       "the existing keyless impersonated ADC must be available.\n")
    if client.project != PRODUCTION_FIRESTORE_PROJECT_ID:
        close = getattr(client, "close", None)
        if callable(close):
            close()
        parser.error("Firestore project does not match the existing TrendScout project")

    try:
        with tempfile.TemporaryDirectory(prefix="trendscout-local-import-") as directory:
            database_path = Path(directory) / "collections.sqlite"
            snapshot = FirestoreCollectionSnapshot(client, database_path)
            snapshot.load()
            backup_path = (_make_backup(backup_directory, database_path)
                           if args.apply else None)
            CollectionStore(database_path, data_mode_scope="LIVE")
            counts = merge_local_database(source_path, database_path)
            print(f"Target: {PRODUCTION_FIRESTORE_PROJECT_ID}/(default)")
            print(f"Base snapshot version: {snapshot.version}")
            print("Rows inserted: " + ", ".join(
                f"{table}={count}" for table, count in sorted(counts.items())
            ))
            if backup_path:
                print(f"Existing snapshot backup: {backup_path}")
            if args.apply:
                version = snapshot.save()
                print(f"Committed snapshot version: {version}")
            else:
                print("DRY RUN: no Firestore snapshot committed; rerun with --apply to import.")
    except Exception as error:
        parser.exit(2, f"Import stopped safely ({type(error).__name__}).\n")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
