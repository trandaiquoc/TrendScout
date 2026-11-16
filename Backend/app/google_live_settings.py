"""Typed, fail-closed settings for the local Google LIVE composition."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def _flag(environment: Mapping[str, str], name: str) -> bool:
    raw = environment.get(name, "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"Invalid boolean setting: {name}")


def _bounded_int(environment: Mapping[str, str], name: str, default: int,
                  low: int, high: int) -> int:
    try:
        value = int(environment.get(name, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid integer setting: {name}") from None
    if not low <= value <= high:
        raise ValueError(f"Out-of-range integer setting: {name}")
    return value


@dataclass(frozen=True)
class GoogleLiveSettings:
    trends_approved: bool = False
    rss_approved: bool = False
    serp_approved: bool = False
    bing_approved: bool = False
    policy_migration_approved: bool = False
    account_api_approved: bool = False
    serpapi_daily_budget: int = 6
    serpapi_monthly_budget: int = 180
    rss_daily_budget: int = 30
    rss_monthly_budget: int = 900
    fetch_starts_per_hour: int = 60
    cross_machine_reserve: int = 6
    bing_daily_budget: int = 3

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None):
        environment = os.environ if environ is None else environ
        return cls(
            trends_approved=_flag(environment, "TREND_SCOUT_GOOGLE_TRENDS_APPROVED"),
            rss_approved=_flag(environment, "TREND_SCOUT_GOOGLE_RSS_APPROVED"),
            serp_approved=_flag(environment, "TREND_SCOUT_GOOGLE_SERP_APPROVED"),
            bing_approved=_flag(environment, "TREND_SCOUT_BING_SERP_APPROVED"),
            policy_migration_approved=_flag(
                environment, "TREND_SCOUT_SERPAPI_BING_POLICY_MIGRATION_APPROVED"
            ),
            account_api_approved=_flag(environment, "TREND_SCOUT_SERPAPI_ACCOUNT_API_APPROVED"),
            serpapi_daily_budget=_bounded_int(
                environment, "TREND_SCOUT_SERPAPI_DAILY_BUDGET", 6, 1, 10
            ),
            serpapi_monthly_budget=_bounded_int(
                environment, "TREND_SCOUT_SERPAPI_MONTHLY_BUDGET", 180, 1, 180
            ),
            rss_daily_budget=_bounded_int(
                environment, "TREND_SCOUT_GOOGLE_RSS_DAILY_BUDGET", 30, 1, 30
            ),
            rss_monthly_budget=_bounded_int(
                environment, "TREND_SCOUT_GOOGLE_RSS_MONTHLY_BUDGET", 900, 1, 900
            ),
            fetch_starts_per_hour=_bounded_int(
                environment, "TREND_SCOUT_GOOGLE_FETCHES_PER_HOUR", 60, 1, 60
            ),
            cross_machine_reserve=_bounded_int(
                environment, "TREND_SCOUT_SERPAPI_CROSS_MACHINE_RESERVE", 6, 0, 1000
            ),
            bing_daily_budget=_bounded_int(
                environment, "TREND_SCOUT_BING_SERPAPI_DAILY_BUDGET", 3, 1, 3
            ),
        )

    @property
    def paid_discovery_approved(self) -> bool:
        return self.trends_approved and self.account_api_approved

    @property
    def paid_serp_approved(self) -> bool:
        return self.serp_approved and self.account_api_approved

    @property
    def paid_bing_approved(self) -> bool:
        return self.bing_approved and self.account_api_approved
