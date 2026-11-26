"""Explicit schema and SQLite-backup migration operations; never cuts over runtime."""

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
    from app.storage.postgres_config import postgres_environment, verify_same_database
    from app.storage.postgres_connection import (
        PostgresConfigurationError,
        PostgresConnectionError,
        postgres_connection,
    )
    from app.storage.postgres_migrations import MigrationMismatch, apply_migrations
    from app.storage.sqlite_scheduler_to_postgres import (
        import_scheduler_backup,
        inspect_scheduler_backup,
    )
    from app.storage.sqlite_to_postgres import (
        ImportConflict,
        InvalidBackup,
        import_backup,
        inspect_backup,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("schema", "dry-run", "import", "scheduler-dry-run", "scheduler-import"))
    parser.add_argument("--schema", required=True, help="explicit target PostgreSQL schema")
    parser.add_argument("--backup", type=Path, help="standalone, consistent SQLite backup")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--env-file", type=Path, default=BACKEND.parent / ".env")
    args = parser.parse_args()
    if args.action != "schema" and args.backup is None:
        parser.error("--backup is required for dry-run and import")
    try:
        values = postgres_environment(os.environ, args.env_file)
        verify_same_database(values)
        with postgres_connection(values, direct=True) as connection:
            connection.autocommit = True
            if args.action == "schema":
                result = {"applied_versions": apply_migrations(connection, schema=args.schema)}
            elif args.action == "dry-run":
                result = inspect_backup(connection, args.backup, schema=args.schema,
                                        batch_size=args.batch_size)
            elif args.action == "import":
                # This imports rows into the named schema only. It neither
                # fences legacy writers nor makes that schema an authority.
                inspect_backup(connection, args.backup, schema=args.schema,
                               batch_size=args.batch_size)
                result = import_backup(connection, args.backup, schema=args.schema,
                                       batch_size=args.batch_size)
            elif args.action == "scheduler-dry-run":
                result = inspect_scheduler_backup(connection, args.backup, schema=args.schema,
                                                  batch_size=args.batch_size)
            else:
                result = import_scheduler_backup(connection, args.backup, schema=args.schema,
                                                 batch_size=args.batch_size)
        print(json.dumps({"status": "ok", "action": args.action, "result": result}))
        return 0
    except Exception as exc:
        # Connection diagnostics and file contents can contain credentials or
        # sensitive data; only explicit sanitized exception types add details.
        result = {"status": "failed", "action": args.action, "error_type": type(exc).__name__}
        if isinstance(exc, (PostgresConfigurationError, PostgresConnectionError,
                            MigrationMismatch, ImportConflict, InvalidBackup)):
            result["detail"] = str(exc)
        print(json.dumps(result), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
