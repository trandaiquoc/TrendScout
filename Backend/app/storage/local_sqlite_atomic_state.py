"""Local-only durable atomic state for offline synthetic Fetch.

SQLite coordinates independent processes on one host and local filesystem. It is
not a multi-host store and cannot be used outside an explicitly local/test,
synthetic runtime.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Callable

from app.ports.atomic_state import AtomicStateStore, State

_KEY = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_MAX_STATE_BYTES = 4 * 1024 * 1024


class LocalSQLiteAtomicStateStore:
    """File-backed atomic store scoped to local/test synthetic executions."""

    def __init__(self, path: str | os.PathLike[str], *, environment_profile: str,
                 data_mode: str = "SYNTHETIC", timeout_seconds: float = 10.0):
        if (environment_profile not in {"local", "test"} or data_mode != "SYNTHETIC"
                or type(timeout_seconds) not in {int, float}
                or not 0.1 <= timeout_seconds <= 60):
            raise ValueError("local SQLite requires an explicit local/test synthetic profile")
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str) or not raw_path or raw_path == ":memory:":
            raise ValueError("persistent local SQLite file path required")
        self.path = Path(raw_path).expanduser().absolute()
        self.timeout_seconds = float(timeout_seconds)
        self._prepare_file()
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS atomic_state "
                "(state_key TEXT PRIMARY KEY, state_json TEXT NOT NULL)"
            )

    def _prepare_file(self) -> None:
        if not self.path.parent.is_dir():
            raise ValueError("local SQLite parent directory must already exist")
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            try:
                metadata = self.path.lstat()
            except OSError as exc:
                raise ValueError("local SQLite file is unavailable") from exc
            if (not self.path.is_file() or self.path.is_symlink()
                    or metadata.st_uid != os.geteuid()):
                raise ValueError("local SQLite file must be a regular file owned by this user")
            os.chmod(self.path, 0o600)
        else:
            os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=self.timeout_seconds, isolation_level=None
        )
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def transact(self, key: str, operation: Callable[[State], tuple[State, object]]):
        if not isinstance(key, str) or not _KEY.fullmatch(key) or not callable(operation):
            raise ValueError("valid local atomic-state key and operation required")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT state_json FROM atomic_state WHERE state_key = ?", (key,)
                ).fetchone()
                current = json.loads(row[0]) if row else {}
                if not isinstance(current, dict):
                    raise ValueError("atomic state must be a JSON object")
                updated, result = operation(current)
                if not isinstance(updated, dict):
                    raise ValueError("atomic state must be a JSON object")
                try:
                    encoded = json.dumps(
                        updated, ensure_ascii=False, allow_nan=False,
                        separators=(",", ":"), sort_keys=True,
                    )
                    size = len(encoded.encode("utf-8"))
                except (TypeError, ValueError, UnicodeError):
                    raise ValueError("atomic state must be finite UTF-8 JSON") from None
                if size > _MAX_STATE_BYTES:
                    raise ValueError("atomic state exceeds local document budget")
                connection.execute(
                    "INSERT INTO atomic_state(state_key, state_json) VALUES (?, ?) "
                    "ON CONFLICT(state_key) DO UPDATE SET state_json = excluded.state_json",
                    (key, encoded),
                )
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise


def local_sqlite_atomic_state(path: str | os.PathLike[str], *, environment_profile: str,
                              data_mode: str = "SYNTHETIC") -> AtomicStateStore:
    """Typed composition helper; keeps business services on the storage port."""
    return LocalSQLiteAtomicStateStore(
        path, environment_profile=environment_profile, data_mode=data_mode
    )
