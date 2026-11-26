"""Create private, standalone backups and aggregate inventory of local SQLite stores.

Run on each Windows host from Backend. The capture never starts Fetch, reads
record values, or writes to a source database. Each SQLite backup is taken via
the online backup API so committed WAL pages are included consistently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import sqlite3
import stat
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class SQLiteSource:
    source_id: str
    path: Path
    location_label: str


def default_sources(*, local_app_data: Path, home: Path, environment=None):
    """Return only the application-owned SQLite stores used on a local host."""
    environment = os.environ if environment is None else environment
    configured_collections = environment.get("TRENDSCOUT_COLLECTIONS_DIR")
    collections_directory = (
        Path(configured_collections)
        if isinstance(configured_collections, str) and configured_collections.strip()
        else local_app_data / "TrendScout" / "collections-v1"
    )
    state_root = home / ".local" / "state" / "trendscout"
    return (
        SQLiteSource("collections", collections_directory / "collections.sqlite",
                     "configured collections dir/collections.sqlite"
                     if configured_collections else "collections-v1/collections.sqlite"),
        SQLiteSource("scheduler", local_app_data / "TrendScout" / "scheduler-v1"
                     / "schedules.sqlite", "scheduler-v1/schedules.sqlite"),
        SQLiteSource("normal_google_live", state_root / "normal-google-live-v1"
                     / "state.sqlite", ".local/state/trendscout/normal-google-live-v1/state.sqlite"),
        SQLiteSource("normal_wikimedia_live", state_root / "normal-wikimedia-live-v1"
                     / "state.sqlite", ".local/state/trendscout/normal-wikimedia-live-v1/state.sqlite"),
        SQLiteSource("d6_smoke", local_app_data / "TrendScout" / "local" / "d6-smoke"
                     / "ledger.sqlite", "local/d6-smoke/ledger.sqlite"),
    )


def _quote_identifier(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("SQLite returned an invalid object name")
    return '"' + value.replace('"', '""') + '"'


def _source_uri(path: Path):
    return path.absolute().as_uri() + "?mode=ro"


def _file_digest(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect_backup(path: Path):
    with sqlite3.connect(path.absolute().as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        integrity = [row[0] for row in db.execute("PRAGMA integrity_check")]
        foreign_key_violations = sum(1 for _ in db.execute("PRAGMA foreign_key_check"))
        tables = []
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            quoted = _quote_identifier(name)
            columns = [
                {"name": row[1], "type": row[2], "primary_key_order": row[5]}
                for row in db.execute(f"PRAGMA table_info({quoted})")
            ]
            count = db.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
            tables.append({"name": name, "row_count": count, "columns": columns})
        return {
            "integrity_check": "ok" if integrity == ["ok"] else "failed",
            "foreign_key_violation_count": foreign_key_violations,
            "tables": tables,
        }


def capture_database(source: SQLiteSource, output_directory: Path):
    """Capture one committed SQLite snapshot, exposing only schema/count metadata."""
    result = {
        "source_id": source.source_id,
        "location_label": source.location_label,
        "status": "missing",
        "backup_file": None,
    }
    path = source.path
    try:
        path.lstat()
    except FileNotFoundError:
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(path) + suffix).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return {**result, "status": "failed", "error_type": "UnreadableSQLiteSidecar"}
            return {**result, "status": "failed", "error_type": "OrphanSQLiteSidecar"}
        return result
    except OSError:
        return {**result, "status": "failed", "error_type": "UnreadableSourcePath"}
    try:
        metadata = path.lstat()
    except OSError:
        return {**result, "status": "failed", "error_type": "UnreadableSourcePath"}
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        return {**result, "status": "failed", "error_type": "UnsafeSourcePath"}

    sidecars = {}
    try:
        from app.storage.local_file_security import verify_private_file

        verify_private_file(path)
        started_at = datetime.now(timezone.utc)
        before = path.stat()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                if sidecar.is_symlink() or not sidecar.is_file():
                    raise ValueError("SQLite sidecar is not a regular file")
                verify_private_file(sidecar)
                sidecars[suffix[1:]] = sidecar.stat().st_size

        backup_path = output_directory / f"{source.source_id}.sqlite"
        if backup_path.exists() or backup_path.is_symlink():
            raise FileExistsError("backup path already exists")
        with closing(sqlite3.connect(_source_uri(path), uri=True, timeout=15)) as source_db:
            source_db.execute("PRAGMA busy_timeout=15000")
            with closing(sqlite3.connect(backup_path, timeout=15)) as backup_db:
                source_db.backup(backup_db, pages=256, sleep=0.05)
        if os.name != "nt":
            os.chmod(backup_path, 0o600)

        after = path.stat()
        inventory = _inspect_backup(backup_path)
        if inventory["integrity_check"] != "ok":
            raise ValueError("standalone backup failed SQLite integrity_check")

        verify_private_file(backup_path)
        finished_at = datetime.now(timezone.utc)
        return {
            **result,
            "status": "captured",
            "backup_file": backup_path.name,
            "backup_sha256": _file_digest(backup_path),
            "backup_size_bytes": backup_path.stat().st_size,
            "source_size_bytes_before": before.st_size,
            "source_size_bytes_after": after.st_size,
            "source_changed_during_capture": (
                before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
            ),
            "source_wal_sidecar_bytes": sidecars.get("wal", 0),
            "source_shm_sidecar_bytes": sidecars.get("shm", 0),
            "capture_started_at": started_at.isoformat(),
            "capture_finished_at": finished_at.isoformat(),
            **inventory,
        }
    except Exception as error:
        backup_path = output_directory / f"{source.source_id}.sqlite"
        if backup_path.is_file() and not backup_path.is_symlink():
            backup_path.unlink(missing_ok=True)
        return {**result, "status": "failed", "error_type": type(error).__name__}


def _write_inventory(path: Path, inventory):
    encoded = json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
    if os.name != "nt":
        os.chmod(path, 0o600)
    from app.storage.local_file_security import verify_private_file

    verify_private_file(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    if os.name != "nt":
        parser.error("this inventory command is intended for Windows hosts")

    local_app_data_value = os.environ.get("LOCALAPPDATA")
    if not local_app_data_value:
        parser.error("LOCALAPPDATA is not set")
    local_app_data = Path(local_app_data_value)
    home = Path.home()
    output_parent = local_app_data / "TrendScout" / "migration-backups"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_directory = output_parent / f"inventory-{timestamp}-{uuid4().hex[:10]}"

    try:
        from app.storage.local_file_security import secure_private_directory

        secure_private_directory(output_directory)
        sources = default_sources(local_app_data=local_app_data, home=home)
        entries = [capture_database(source, output_directory) for source in sources]
        captured = sum(item["status"] == "captured" for item in entries)
        failed = sum(item["status"] == "failed" for item in entries)
        inventory = {
            "inventory_version": 1,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "status": "failed" if failed else "ok" if captured else "no_databases_found",
            "database_count": len(entries),
            "captured_database_count": captured,
            "missing_database_count": sum(item["status"] == "missing" for item in entries),
            "failed_database_count": failed,
            "databases": entries,
            "contents_note": (
                "Standalone SQLite backups and aggregate schema/count metadata only; "
                "no row values or credentials are included in inventory.json. "
                "Each database is consistent at its own capture time; the set is "
                "not a cross-database transaction snapshot."
            ),
        }
        _write_inventory(output_directory / "inventory.json", inventory)
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        return 1

    print(json.dumps({
        "status": inventory["status"],
        "output_directory": str(output_directory),
        "captured_database_count": captured,
        "missing_database_count": inventory["missing_database_count"],
        "failed_database_count": failed,
    }, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
