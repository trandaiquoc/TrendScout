"""Atomically initialize shared AI quota from owner-reconciled evidence.

This operator command writes only the explicitly named PostgreSQL AI state
scope. It does not call an AI provider, reset counters, delete reservations,
run migrations, or change runtime configuration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.ai.runtime import AIConfig, AIService
    from app.storage.local_file_security import verify_private_file
    from app.storage.postgres_atomic_state import PostgresAtomicStateStore
    from app.storage.postgres_config import postgres_environment, verify_same_database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="explicit PostgreSQL application schema")
    parser.add_argument("--evidence-file", required=True, type=Path,
                        help="owner-only JSON with provider account/quota/cooldown reconciliation")
    parser.add_argument("--env-file", type=Path, default=BACKEND.parent / ".env")
    args = parser.parse_args(argv)

    try:
        evidence_path = args.evidence_file.absolute()
        verify_private_file(evidence_path)
        if evidence_path.stat().st_size > 32_768:
            raise ValueError("quota evidence file is too large")
        with evidence_path.open("r", encoding="utf-8") as stream:
            evidence = json.load(stream)

        # The root env file is the selected source of truth; process variables
        # must not silently replace its rotated database credentials.
        database_environment = postgres_environment({}, args.env_file)
        verify_same_database(database_environment)
        store = PostgresAtomicStateStore(database_environment, schema=args.schema)
        service = AIService(
            store, config=AIConfig(enabled=False, api_key_present=False), provider=object(),
        )
        result = service.bootstrap_quota(evidence)
        print(json.dumps({
            "status": "ok", "quota_status": result["status"],
            "changed": result["changed"],
        }, sort_keys=True))
        return 0
    except Exception as exc:
        # Exceptions are intentionally reduced to their type; database and
        # evidence errors may otherwise embed sensitive configuration values.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
