"""Load the repository-root dotenv file for the local runtime only."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import MutableMapping

from app.storage.local_file_security import UnsafeLocalFile, verify_private_file

_MAX_ENV_BYTES = 64 * 1024
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LocalEnvironmentError(ValueError):
    """The local environment file is malformed or cannot be read safely."""


def _parse_environment(raw: bytes) -> dict[str, str]:
    try:
        contents = raw.decode("utf-8-sig")
    except UnicodeError:
        raise LocalEnvironmentError("local environment file is not valid UTF-8") from None

    values = {}
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not _NAME.fullmatch(name) or name in values:
            raise LocalEnvironmentError(
                f"local environment file has an invalid or duplicate assignment on line {line_number}"
            )

        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing < 0:
                raise LocalEnvironmentError(
                    f"local environment file has an invalid quoted value on line {line_number}"
                )
            trailing = value[closing + 1:].strip()
            if trailing and not trailing.startswith("#"):
                raise LocalEnvironmentError(
                    f"local environment file has trailing content on line {line_number}"
                )
            value = value[1:closing]
        else:
            comment = value.find(" #")
            if comment >= 0:
                value = value[:comment].rstrip()

        if "\x00" in value:
            raise LocalEnvironmentError(
                f"local environment file has an invalid value on line {line_number}"
            )
        values[name] = value
    return values


def load_local_environment(
    env_file: str | os.PathLike[str],
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Fill missing process variables from an explicit local dotenv path.

    Existing process variables, including intentionally empty values, always win.
    The return value contains variable names only and never contains their values.
    """
    environment = os.environ if environ is None else environ
    path = Path(env_file)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return ()
    except OSError:
        raise LocalEnvironmentError("local environment file is unavailable or unsafe") from None

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_ENV_BYTES:
            raise LocalEnvironmentError("local environment file is not a bounded regular file")
        verify_private_file(path)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(_MAX_ENV_BYTES + 1)
        if len(raw) > _MAX_ENV_BYTES:
            raise LocalEnvironmentError("local environment file is too large")
        values = _parse_environment(raw)
    except (OSError, UnsafeLocalFile):
        raise LocalEnvironmentError("local environment file is unavailable or unsafe") from None
    finally:
        if descriptor != -1:
            os.close(descriptor)

    loaded = []
    for name, value in values.items():
        if name not in environment:
            environment[name] = value
            loaded.append(name)
    return tuple(loaded)
