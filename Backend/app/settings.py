"""Configuration read from the process environment."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(app_name=os.getenv("TREND_SCOUT_APP_NAME", "TrendScout API"))
