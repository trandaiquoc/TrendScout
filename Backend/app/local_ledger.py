"""One server-owned ledger scope for the personal live demo and smoke CLI."""

from pathlib import Path

DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "local" / "d6-smoke"
STATE_KEY = "d6-smoke"
