"""Crash-released, non-blocking local process locks for Windows and POSIX."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from app.storage.local_file_security import (
    secure_private_directory,
    verify_private_file,
)


class LocalProcessLock:
    """One byte-range lock on Windows or an exclusive flock on POSIX."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        secure_private_directory(self.path.parent)
        flags = os.O_CREAT | os.O_RDWR
        if os.name != "nt":
            flags |= os.O_NOFOLLOW
        else:
            flags |= getattr(os, "O_BINARY", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("local lock must be a regular file")
            verify_private_file(self.path)
            if os.name == "nt":
                import msvcrt

                if info.st_size == 0:
                    os.write(fd, b"\0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EDEADLK, errno.EAGAIN, errno.EWOULDBLOCK):
                        return self._close_and_return(fd, False)
                    raise
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return self._close_and_return(fd, False)
            self.fd = fd
            return True
        except BaseException:
            if self.fd != fd:
                os.close(fd)
            raise

    @staticmethod
    def _close_and_return(fd: int, result: bool) -> bool:
        os.close(fd)
        return result

    def release(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
