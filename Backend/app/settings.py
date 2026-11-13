"""Typed, validated application configuration read from one environment boundary."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from app.sources.catalog import DEFAULT_WIKIMEDIA_USER_AGENT, SOURCE_CAPABILITIES
from app.sources.http_policy import HttpPolicy
from app.sources.query_plan import DEFAULT_QUERY_PLAN, get_query_plan

_ALLOWED_PROFILES = frozenset({"local", "development", "test", "production", "vercel"})
_SOURCE_ENV_NAMES = {
    "wikimedia_pageviews_top_per_country": "TREND_SCOUT_SOURCE_WIKIMEDIA_ENABLED",
    "youtube_video_search": "TREND_SCOUT_SOURCE_YOUTUBE_ENABLED",
    "google_web_serp": "TREND_SCOUT_SOURCE_GOOGLE_ENABLED",
    "bing_web_serp": "TREND_SCOUT_SOURCE_BING_ENABLED",
}


def _boolean(environment: Mapping[str, str], key: str, default: bool) -> bool:
    raw = environment.get(key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"Invalid boolean setting: {key}")


def _integer(environment: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(environment.get(key, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid integer setting: {key}") from None
    if not low <= value <= high:
        raise ValueError(f"Out-of-range integer setting: {key}")
    return value


def _bounded_float(
    environment: Mapping[str, str], key: str, default: float, minimum: float, maximum: float
) -> float:
    try:
        value = float(environment.get(key, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid numeric setting: {key}") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"Out-of-range numeric setting: {key}")
    return value


def _positive_float(environment: Mapping[str, str], key: str, default: float) -> float:
    return _bounded_float(environment, key, default, 0.000001, 60.0)


@dataclass(frozen=True)
class Settings:
    app_name: str
    environment_profile: str
    read_only: bool
    run_mode: str
    enabled_source_ids: frozenset[str]
    source_endpoints: Mapping[str, str]
    provider_ids: Mapping[str, str]
    credential_references: Mapping[str, str | None]
    default_market: str
    default_language: str
    query_plan_id: str
    query_plan_version: int
    query_request_budget_per_source: int
    http_policy: HttpPolicy
    google_cloud_project: str | None
    google_wif_audience: str | None = field(repr=False)
    operator_token: str | None = field(repr=False)
    read_password: str | None = field(repr=False)
    lock_path: str
    run_cooldown_seconds: int
    wikimedia_user_agent: str
    allowed_fetch_operations: frozenset[str]
    public_fetch_enabled: bool = False
    public_fetch_deadline_seconds: float = 60.0
    scheduler_enabled: bool = False

    def __post_init__(self):
        if self.environment_profile not in _ALLOWED_PROFILES:
            raise ValueError("Unsupported environment profile")
        if self.run_mode not in {"live", "synthetic", "replay"}:
            raise ValueError("Unsupported run mode")
        if type(self.read_only) is not bool:
            raise ValueError("Read-only setting must be boolean")
        expected_endpoints = {item.source_id: item.endpoint for item in SOURCE_CAPABILITIES}
        expected_providers = {item.source_id: item.provider_id for item in SOURCE_CAPABILITIES}
        expected_credentials = {
            item.source_id: item.credential_reference for item in SOURCE_CAPABILITIES
        }
        if set(self.source_endpoints) != set(expected_endpoints):
            raise ValueError("Settings must include the complete source endpoint catalog")
        if dict(self.source_endpoints) != expected_endpoints:
            raise ValueError("Source endpoints must match the centralized provider catalog")
        if dict(self.provider_ids) != expected_providers:
            raise ValueError("Provider IDs must match the centralized source catalog")
        if dict(self.credential_references) != expected_credentials:
            raise ValueError("Credential references must match the centralized source catalog")
        enabled_sources = frozenset(self.enabled_source_ids)
        implemented_sources = {
            item.source_id for item in SOURCE_CAPABILITIES if item.readiness == "IMPLEMENTED"
        }
        if not enabled_sources <= implemented_sources:
            raise ValueError("Only implemented and approved sources can be enabled")
        object.__setattr__(self, "enabled_source_ids", enabled_sources)
        if not isinstance(self.http_policy, HttpPolicy):
            raise ValueError("Settings require a validated HTTP policy")
        if (
            type(self.run_cooldown_seconds) is not int
            or not 0 <= self.run_cooldown_seconds <= 86400
        ):
            raise ValueError("Run cooldown must be between 0 and 86400 seconds")
        if type(self.public_fetch_enabled) is not bool:
            raise ValueError("Public Fetch setting must be boolean")
        if (
            isinstance(self.public_fetch_deadline_seconds, bool)
            or not isinstance(self.public_fetch_deadline_seconds, (int, float))
            or not 1.0 <= self.public_fetch_deadline_seconds <= 300.0
        ):
            raise ValueError("Public Fetch deadline must be between 1 and 300 seconds")
        if self.public_fetch_enabled and (
            self.environment_profile not in {"local", "test"}
            or self.run_mode != "synthetic"
            or self.read_only
            or self.google_cloud_project
        ):
            raise ValueError("Public Fetch is limited to local offline synthetic profiles")
        if type(self.scheduler_enabled) is not bool:
            raise ValueError("Scheduler setting must be boolean")
        if self.scheduler_enabled and (
            self.environment_profile not in {"local", "test"}
            or self.run_mode != "synthetic"
            or not self.public_fetch_enabled
            or self.read_only
            or self.google_cloud_project
            or not self.operator_token
            or len(self.operator_token) < 24
        ):
            raise ValueError("Scheduler requires an operator and enabled local synthetic Fetch")
        if self.read_only and self.allowed_fetch_operations:
            raise ValueError("Read-only settings cannot allow fetch operations")
        operations = frozenset(self.allowed_fetch_operations)
        if not operations <= {"manual_run"}:
            raise ValueError("Unsupported fetch operation")
        if "manual_run" in operations and (
            not self.operator_token or len(self.operator_token) < 24
        ):
            raise ValueError("Manual run requires a configured operator token")
        object.__setattr__(self, "allowed_fetch_operations", operations)
        plan = get_query_plan(self.query_plan_id, self.query_plan_version)
        if (
            self.default_market != plan.market
            or self.default_language != plan.language
            or self.query_request_budget_per_source != plan.query_request_budget_per_source
        ):
            raise ValueError("Query settings must match the selected versioned query plan")
        if not self.lock_path or not self.wikimedia_user_agent:
            raise ValueError("Lock path and Wikimedia User-Agent are required")
        object.__setattr__(self, "source_endpoints", MappingProxyType(dict(self.source_endpoints)))
        object.__setattr__(self, "provider_ids", MappingProxyType(dict(self.provider_ids)))
        object.__setattr__(
            self, "credential_references", MappingProxyType(dict(self.credential_references))
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Parse process configuration or an injected mapping without creating clients."""
        environment = os.environ if environ is None else environ
        app_name = environment.get("TREND_SCOUT_APP_NAME", "TrendScout API").strip()
        if not app_name or len(app_name) > 120:
            raise ValueError("TREND_SCOUT_APP_NAME must contain 1 to 120 characters")

        raw_profile = environment.get("TREND_SCOUT_ENVIRONMENT", "local").strip().lower()
        if raw_profile not in _ALLOWED_PROFILES:
            raise ValueError("Unsupported TREND_SCOUT_ENVIRONMENT profile")
        is_vercel = environment.get("VERCEL", "") == "1" or raw_profile == "vercel"
        profile = "vercel" if is_vercel else raw_profile
        read_only = is_vercel or _boolean(environment, "TREND_SCOUT_READ_ONLY", False)
        run_mode = environment.get("TREND_SCOUT_RUN_MODE", "live").strip().lower()
        if run_mode not in {"live", "synthetic", "replay"}:
            raise ValueError("Unsupported TREND_SCOUT_RUN_MODE")

        enabled_source_ids = set()
        for capability in SOURCE_CAPABILITIES:
            env_name = _SOURCE_ENV_NAMES[capability.source_id]
            enabled = _boolean(environment, env_name, capability.default_enabled)
            if enabled and capability.readiness != "IMPLEMENTED":
                raise ValueError(f"Source is not approved and implemented: {capability.source_id}")
            if enabled:
                enabled_source_ids.add(capability.source_id)

        connect_timeout = _positive_float(
            environment, "TREND_SCOUT_HTTP_CONNECT_TIMEOUT_SECONDS", 5.0
        )
        read_timeout = _positive_float(environment, "TREND_SCOUT_HTTP_READ_TIMEOUT_SECONDS", 15.0)
        total_timeout = _positive_float(environment, "TREND_SCOUT_HTTP_TOTAL_TIMEOUT_SECONDS", 15.0)
        max_retries = _integer(environment, "TREND_SCOUT_HTTP_MAX_RETRIES", 2, 0, 5)
        max_retry_wait = _bounded_float(
            environment, "TREND_SCOUT_HTTP_MAX_RETRY_WAIT_SECONDS", 30.0, 0.0, 300.0
        )
        http_policy = HttpPolicy(
            connect_timeout_seconds=connect_timeout,
            read_timeout_seconds=read_timeout,
            total_timeout_seconds=total_timeout,
            max_retries=max_retries,
            max_retry_wait_seconds=max_retry_wait,
        )

        plan_id = environment.get("TREND_SCOUT_QUERY_PLAN_ID", DEFAULT_QUERY_PLAN.plan_id).strip()
        plan_version = _integer(
            environment, "TREND_SCOUT_QUERY_PLAN_VERSION", DEFAULT_QUERY_PLAN.version, 1, 100
        )
        query_plan = get_query_plan(plan_id, plan_version)

        lock_path = environment.get("TREND_SCOUT_LOCK_PATH")
        if lock_path is None:
            try:
                home = Path.home()
            except RuntimeError:
                home = Path(environment.get("LOCALAPPDATA") or Path.cwd())
            lock_path = str(home / ".cache/trendscout/manual-run.lock")
        if not lock_path.strip():
            raise ValueError("TREND_SCOUT_LOCK_PATH must not be empty")

        endpoints = MappingProxyType(
            {capability.source_id: capability.endpoint for capability in SOURCE_CAPABILITIES}
        )
        providers = MappingProxyType(
            {capability.source_id: capability.provider_id for capability in SOURCE_CAPABILITIES}
        )
        credentials = MappingProxyType(
            {
                capability.source_id: capability.credential_reference
                for capability in SOURCE_CAPABILITIES
            }
        )
        user_agent = environment.get("WIKIMEDIA_USER_AGENT", DEFAULT_WIKIMEDIA_USER_AGENT).strip()
        if not user_agent or len(user_agent) > 255 or "\n" in user_agent or "\r" in user_agent:
            raise ValueError("Invalid WIKIMEDIA_USER_AGENT")

        operator_token = environment.get("TREND_SCOUT_OPERATOR_TOKEN") or None
        public_fetch_enabled = _boolean(
            environment, "TREND_SCOUT_PUBLIC_FETCH_ENABLED", False
        )
        public_fetch_deadline_seconds = _bounded_float(
            environment, "TREND_SCOUT_PUBLIC_FETCH_DEADLINE_SECONDS", 60.0, 1.0, 300.0
        )
        scheduler_enabled = _boolean(environment, "TREND_SCOUT_SCHEDULER_ENABLED", False)
        allowed_fetch_operations = (
            frozenset({"manual_run"})
            if not read_only and operator_token and len(operator_token) >= 24
            else frozenset()
        )
        return cls(
            app_name=app_name,
            environment_profile=profile,
            read_only=read_only,
            run_mode=run_mode,
            enabled_source_ids=frozenset(enabled_source_ids),
            source_endpoints=endpoints,
            provider_ids=providers,
            credential_references=credentials,
            default_market=query_plan.market,
            default_language=query_plan.language,
            query_plan_id=query_plan.plan_id,
            query_plan_version=query_plan.version,
            query_request_budget_per_source=query_plan.query_request_budget_per_source,
            http_policy=http_policy,
            google_cloud_project=environment.get("GOOGLE_CLOUD_PROJECT") or None,
            google_wif_audience=environment.get("GOOGLE_WIF_AUDIENCE") or None,
            operator_token=operator_token,
            read_password=environment.get("TREND_SCOUT_READ_PASSWORD") or None,
            lock_path=lock_path,
            run_cooldown_seconds=_integer(
                environment, "TREND_SCOUT_RUN_COOLDOWN_SECONDS", 60, 0, 86400
            ),
            wikimedia_user_agent=user_agent,
            allowed_fetch_operations=allowed_fetch_operations,
            public_fetch_enabled=public_fetch_enabled,
            public_fetch_deadline_seconds=public_fetch_deadline_seconds,
            scheduler_enabled=scheduler_enabled,
        )
