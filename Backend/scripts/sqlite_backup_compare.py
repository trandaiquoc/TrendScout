"""Compare two standalone SQLite backups and print aggregate differences only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def main() -> int:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.storage.sqlite_backup_compare import compare_backups
    from app.storage.sqlite_to_postgres import InvalidBackup

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", required=True, type=Path, help="local standalone SQLite backup")
    parser.add_argument("--production", required=True, type=Path,
                        help="Production standalone SQLite backup")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    try:
        report = compare_backups(args.local, args.production, batch_size=args.batch_size)
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, (InvalidBackup, ValueError)):
            result["detail"] = str(exc)
        print(json.dumps(result, sort_keys=True))
        return 1
    print(json.dumps({"status": "ok", "report": report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
