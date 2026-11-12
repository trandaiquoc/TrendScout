"""Read personal demo credentials from an owner-only project .env file.

This resolver does not enable a provider or prove that a credential works.
"""

import os
import stat
from pathlib import Path

from app.storage.local_file_security import (
    UnsafeLocalFile,
    verify_private_directory,
    verify_private_file,
)

REFERENCES = frozenset({"YOUTUBE_API_KEY", "SERPAPI_API_KEY"})
MAX_FILE_SIZE = 8192


class InvalidCredentialConfig(ValueError):
    """Configuration is unsafe or malformed; never include file contents."""


class LocalCredentialProvider:
    def __init__(self, path: Path | None = None):
        if path is None and os.name == "nt":
            app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            path = app_data / "TrendScout" / ".env"
        self.path = path or Path(__file__).resolve().parents[2] / ".env"

    def _read(self):
        try:
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            if os.name != "nt":
                flags |= os.O_NOFOLLOW
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return {}
        except OSError:
            raise InvalidCredentialConfig("Credential file unavailable or unsafe") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_SIZE:
                raise InvalidCredentialConfig("Credential file permissions or type are unsafe")
            try:
                if os.name == "nt":
                    verify_private_directory(self.path.parent)
                verify_private_file(self.path)
            except UnsafeLocalFile:
                raise InvalidCredentialConfig(
                    "Credential file permissions or type are unsafe"
                ) from None
            if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise InvalidCredentialConfig("Credential file permissions or type are unsafe")
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                raw = stream.read(MAX_FILE_SIZE + 1)
            if len(raw) > MAX_FILE_SIZE:
                raise InvalidCredentialConfig("Credential file is too large")
            try:
                lines = raw.decode("utf-8").splitlines()
            except UnicodeError:
                raise InvalidCredentialConfig("Credential file is not UTF-8") from None
            values = {}
            for line in lines:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                name, separator, value = line.partition("=")
                if name not in REFERENCES:
                    if any(ref in name for ref in REFERENCES):
                        raise InvalidCredentialConfig("Credential file format is invalid")
                    continue
                if not separator or name in values:
                    raise InvalidCredentialConfig("Credential file format is invalid")
                if value and (not 8 <= len(value) <= 256
                              or any(ord(char) < 33 or ord(char) > 126 for char in value)):
                    raise InvalidCredentialConfig("Credential value format is invalid")
                values[name] = value or None
            return values
        finally:
            if fd != -1:
                os.close(fd)

    def resolve(self, reference: str) -> str | None:
        if reference not in REFERENCES:
            return None
        return self._read().get(reference)

    def status(self) -> dict[str, str]:
        try:
            values = self._read()
        except InvalidCredentialConfig:
            return {name: "INVALID_CONFIG" for name in sorted(REFERENCES)}
        return {name: "PRESENT" if values.get(name) else "MISSING"
                for name in sorted(REFERENCES)}
