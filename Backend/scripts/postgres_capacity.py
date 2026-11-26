"""Print a read-only PostgreSQL size and connection report."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def main() -> int:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.storage.postgres_capacity import inspect_capacity
    from app.storage.postgres_config import (
        PostgresConfigurationError,
        PostgresConnectionError,
        postgres_environment,
        verify_same_database,
    )
    from app.storage.postgres_connection import postgres_connection
    from app.storage.postgres_migrations import MigrationMismatch

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default="trendscout", help="versioned PostgreSQL schema")
    parser.add_argument("--top", type=int, default=10, help="largest relations to include (1–100)")
    parser.add_argument("--env-file", type=Path, default=BACKEND.parent / ".env")
    args = parser.parse_args()
    try:
        values = postgres_environment(os.environ, args.env_file)
        verify_same_database(values)
        with postgres_connection(values, direct=True) as connection:
            report = inspect_capacity(connection, schema=args.schema, top_n=args.top)
        print(json.dumps({"status": "ok", "report": report}, sort_keys=True))
        return 0
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, (PostgresConfigurationError, PostgresConnectionError,
                            MigrationMismatch, ValueError)):
            result["detail"] = str(exc)
        print(json.dumps(result, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
