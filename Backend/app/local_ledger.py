"""One server-owned ledger scope for the personal live demo and smoke CLI."""

import os
from pathlib import Path

if os.name == "nt":
    app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    DIRECTORY = app_data / "TrendScout" / "local" / "d6-smoke"
else:
    DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "local" / "d6-smoke"
STATE_KEY = "d6-smoke"
