"""Create a private row-level review from two standalone Collections backups."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.storage.sqlite_backup_review import write_reconciliation_review

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", required=True, type=Path,
                        help="owner-only local standalone Collections backup")
    parser.add_argument("--production", required=True, type=Path,
                        help="owner-only Production standalone Collections backup")
    parser.add_argument("--output", required=True, type=Path,
                        help="new JSONL file in an existing owner-only directory")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    try:
        result = write_reconciliation_review(
            args.local, args.production, args.output, batch_size=args.batch_size
        )
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, (ValueError, OSError)):
            result["detail"] = str(exc)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1

    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
