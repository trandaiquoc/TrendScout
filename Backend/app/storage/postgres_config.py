"""Local PostgreSQL configuration and database-identity verification."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping

import psycopg

from app.storage.local_file_security import UnsafeLocalFile, verify_private_file
from app.storage.postgres_connection import (
    PostgresConfigurationError,
    PostgresConnectionError,
    postgres_connection,
)

_NAMES = ("DATABASE_URL", "DATABASE_URL_UNPOOLED")
_MAX_ENV_BYTES = 16_384


def postgres_environment(environment: Mapping[str, str], env_file: Path | None = None) -> dict[str, str]:
    """Resolve only database URLs; never rewrite or print the source file."""
    values = {}
    if env_file is not None and env_file.exists():
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(env_file, flags)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_ENV_BYTES:
                    raise PostgresConfigurationError("database environment file is unsafe")
                verify_private_file(env_file)
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    raw = stream.read(_MAX_ENV_BYTES + 1)
                if len(raw) > _MAX_ENV_BYTES:
                    raise PostgresConfigurationError("database environment file is too large")
                for line in raw.decode("utf-8-sig").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    name, separator, value = line.removeprefix("export ").partition("=")
                    name = name.strip()
                    if name not in _NAMES:
                        continue
                    if not separator or name in values:
                        raise PostgresConfigurationError("duplicate or invalid database configuration")
                    value = value.strip()
                    if value.startswith(("'", '"')) or value.endswith(("'", '"')):
                        if len(value) < 2 or value[0] not in {"'", '"'} or value[-1] != value[0]:
                            raise PostgresConfigurationError("invalid quoted database configuration")
                        value = value[1:-1]
                    values[name] = value
            finally:
                if descriptor != -1:
                    os.close(descriptor)
        except (OSError, UnicodeError, UnsafeLocalFile):
            raise PostgresConfigurationError("database environment file is unavailable or unsafe") from None
    for name in _NAMES:
        if environment.get(name):
            values[name] = environment[name]
        if not isinstance(values.get(name), str) or not values[name].strip():
            raise PostgresConfigurationError(f"{name} is required")
    return values


def verify_same_database(environment: Mapping[str, str]) -> None:
    """Check pooled/direct host, PostgreSQL cluster identifier and database.

    Neon pooled hostnames have a `-pooler` suffix on the first DNS label. A
    successful comparison does not prove Vercel's independently configured
    production secrets or project-level capacity.
    """
    identities = []
    for direct in (False, True):
        with postgres_connection(environment, direct=direct) as connection:
            try:
                cluster, database = connection.execute(
                    "SELECT system_identifier, current_database() FROM pg_control_system()"
                ).fetchone()
            except psycopg.Error:
                raise PostgresConnectionError("database identity query failed") from None
            host = connection.info.host
            if not isinstance(host, str) or not host:
                raise PostgresConfigurationError("database endpoint identity is unavailable")
            label, separator, remainder = host.lower().partition(".")
            normalized_host = label.removesuffix("-pooler") + separator + remainder
            identities.append((normalized_host, cluster, database))
    if identities[0] != identities[1]:
        raise PostgresConfigurationError("pooled and direct URLs do not identify the same database")
