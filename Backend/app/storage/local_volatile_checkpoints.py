"""Keep local demo source checkpoints in memory while persisting quota receipts.

This is a single-process local demo boundary, not a durable or multi-host store.
A restart loses results; reservations remain on disk to prevent repeat requests.
"""

import fcntl
import os
import stat
import threading
from copy import deepcopy
from datetime import datetime, timezone

from app.storage.local_sqlite_atomic_state import LocalSQLiteAtomicStateStore


class LocalVolatileCheckpoints:
    def __init__(self, store, *, clock=lambda: datetime.now(timezone.utc)):
        if (type(store) is not LocalSQLiteAtomicStateStore or store.data_mode != "LIVE"
                or not callable(clock)):
            raise ValueError("local live SQLite store and clock required")
        self.store, self.clock = store, clock
        self._mutex = threading.RLock()
        self._values = {}
        self._closed = False
        self._stop = threading.Event()
        lock_path = store.path.with_name(store.path.name + ".process-lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("unsafe local demo lock file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd
        self._sweeper = threading.Thread(target=self._sweep_loop, daemon=True)
        self._sweeper.start()

    def _prune(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            self._values.clear()
            return
        for key in list(self._values):
            for job in list(self._values[key]):
                entries = self._values[key][job]
                for source in list(entries):
                    try:
                        expiry = datetime.fromisoformat(entries[source]["retain_until"])
                        if expiry.tzinfo is None or expiry.utcoffset() is None or expiry <= now:
                            del entries[source]
                    except (KeyError, TypeError, ValueError):
                        del entries[source]
                if not entries:
                    del self._values[key][job]
            if not self._values[key]:
                del self._values[key]

    def _sweep_loop(self):
        while not self._stop.wait(1):
            with self._mutex:
                try:
                    self._prune()
                except Exception:
                    self._values.clear()

    def transact(self, key, operation):
        with self._mutex:
            if self._closed:
                raise ValueError("local demo store is closed")
            self._prune()
            staged = {}

            def isolated(state):
                jobs = state.get("jobs", {})
                if not isinstance(jobs, dict):
                    raise ValueError("invalid local demo state")
                for job_id, job in jobs.items():
                    if not isinstance(job, dict) or job.get("source_checkpoints"):
                        raise ValueError("persistent source checkpoints are not allowed")
                    checkpoints = self._values.get(key, {}).get(job_id)
                    if checkpoints:
                        job["source_checkpoints"] = deepcopy(checkpoints)
                updated, result = operation(state)
                jobs = updated.get("jobs", {})
                if not isinstance(jobs, dict):
                    raise ValueError("invalid local demo state")
                for job_id, job in jobs.items():
                    if not isinstance(job, dict):
                        raise ValueError("invalid local demo job")
                    checkpoints = job.pop("source_checkpoints", {})
                    if not isinstance(checkpoints, dict):
                        raise ValueError("invalid volatile checkpoints")
                    if checkpoints:
                        staged[job_id] = deepcopy(checkpoints)
                return updated, result

            result = self.store.transact(key, isolated)
            if staged:
                self._values[key] = staged
            else:
                self._values.pop(key, None)
            return result

    def purge_source(self, source_id):
        """Remove this process's retained checkpoints for a source.

        This is a local operator primitive, not a user-request workflow or
        secure erasure guarantee. Durable quota receipts are intentionally kept.
        """
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("source ID required")
        with self._mutex:
            if self._closed:
                raise ValueError("local demo store is closed")
            removed = 0
            for key in list(self._values):
                for job in list(self._values[key]):
                    entries = self._values[key][job]
                    if source_id in entries:
                        del entries[source_id]
                        removed += 1
                    if not entries:
                        del self._values[key][job]
                if not self._values[key]:
                    del self._values[key]
            return removed

    def close(self):
        self._stop.set()
        self._sweeper.join(timeout=2)
        with self._mutex:
            if not self._closed:
                self._values.clear()
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                self._closed = True
