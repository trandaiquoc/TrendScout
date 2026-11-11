"""Wikimedia top-per-country adapter. No topic or language filter is applied."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app.models.observations import (
    CollectionResult,
    DatasetSnapshot,
    FetchStatus,
    ObservationStatus,
    PageObservation,
)
from app.settings import Settings
from app.sources.base import SourceMetadata
from app.sources.catalog import DEFAULT_WIKIMEDIA_USER_AGENT, WIKIMEDIA_SOURCE_ID, source_capability
from app.sources.http_policy import HttpDeadlineExceeded, HttpPolicy

SOURCE = WIKIMEDIA_SOURCE_ID
BASE_URL = source_capability(SOURCE).endpoint
DEFAULT_USER_AGENT = DEFAULT_WIKIMEDIA_USER_AGENT
METADATA = SourceMetadata(
    source_id=SOURCE,
    name="Wikimedia Pageviews top per country",
    country_scope="US",
    access="all-access",
    metric="views_ceil",
    metric_description="Approximate upper-bound pageviews in the country top list",
    capabilities=("daily_snapshot", "latest_available", "seven_day_history", "top_list_censored"),
    configuration=(
        "WIKIMEDIA_USER_AGENT",
        "TREND_SCOUT_HTTP_CONNECT_TIMEOUT_SECONDS",
        "TREND_SCOUT_HTTP_READ_TIMEOUT_SECONDS",
        "TREND_SCOUT_HTTP_TOTAL_TIMEOUT_SECONDS",
        "TREND_SCOUT_HTTP_MAX_RETRIES",
        "TREND_SCOUT_HTTP_MAX_RETRY_WAIT_SECONDS",
    ),
)
_PROJECT = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\Z")
_WIKIMEDIA_DOMAINS = frozenset({"wikipedia", "wikimedia", "wiktionary", "wikiquote", "wikibooks", "wikinews", "wikisource", "wikiversity", "wikivoyage", "wikidata", "mediawiki", "wikifunctions"})


def observation_status(snapshot: DatasetSnapshot, observations: list[PageObservation], project: str, article: str) -> ObservationStatus:
    if any(row.project == project and row.article_original == article for row in observations):
        return ObservationStatus.OBSERVED
    if snapshot.fetch_status == FetchStatus.SUCCESS:
        return ObservationStatus.NOT_IN_TOP
    if snapshot.fetch_status == FetchStatus.DAY_UNAVAILABLE:
        return ObservationStatus.DAY_UNAVAILABLE
    return ObservationStatus.FETCH_ERROR


def _retry_delay(value: str | None, attempt: int, now: datetime) -> float:
    """Compatibility wrapper for the former adapter-local retry helper."""
    return HttpPolicy.parse_retry_after(value, attempt, now)


def _parse(payload: object, day: date, fetched_at: datetime) -> tuple[list[PageObservation], int, int, tuple[object, ...]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or len(payload["items"]) != 1:
        raise ValueError("invalid items schema")
    item = payload["items"][0]
    if not isinstance(item, dict) or item.get("country") != "US" or item.get("access") != "all-access":
        raise ValueError("invalid country or access")
    if (item.get("year"), item.get("month"), item.get("day")) != (day.strftime("%Y"), day.strftime("%m"), day.strftime("%d")):
        raise ValueError("invalid or mismatched observation date")
    articles = item.get("articles")
    if not isinstance(articles, list):
        raise ValueError("missing or invalid articles")
    observations = []
    rejected = 0
    rejected_records = []
    for row in articles:
        if not isinstance(row, dict):
            rejected += 1
            rejected_records.append(row)
            continue
        article, project, views, rank = (row.get(k) for k in ("article", "project", "views_ceil", "rank"))
        if (not isinstance(article, str) or not article or not isinstance(project, str) or not _PROJECT.fullmatch(project)
                or type(views) is not int or views < 0 or type(rank) is not int or rank < 1):
            rejected += 1
            rejected_records.append(dict(row))
            continue
        known_domain = project.rsplit(".", 1)[-1] in _WIKIMEDIA_DOMAINS
        flags = ("TOP_LIST_ONLY", "VIEWS_APPROXIMATE") + (() if known_domain else ("UNVERIFIED_PROJECT_URL",))
        observations.append(PageObservation(
            source=SOURCE, country_scope="US", access="all-access", observation_date=day,
            project=project, article_original=article, article_display=article.replace("_", " "),
            canonical_keyword=article.replace("_", " "), views_ceil=views, provider_rank=rank,
            fetched_at=fetched_at, source_url=f"https://{project}.org/wiki/{quote(article, safe='')}" if known_domain else None,
            raw_record=dict(row), quality_flags=flags,
        ))
    return observations, len(articles), rejected, tuple(rejected_records)


class WikimediaAdapter:
    metadata = METADATA

    def __init__(self, client: httpx.AsyncClient | None = None, *, user_agent: str | None = None,
                 max_retries: int | None = None, max_retry_wait: float | None = None,
                 timeout: float | None = None, http_policy: HttpPolicy | None = None,
                 settings: Settings | None = None):
        if settings is None:
            settings = Settings.from_environment()
        if SOURCE not in settings.enabled_source_ids:
            raise ValueError("Wikimedia source is disabled")
        if user_agent is None:
            user_agent = settings.wikimedia_user_agent
        self.user_agent = user_agent
        if not self.user_agent.strip() or "\n" in self.user_agent or "\r" in self.user_agent:
            raise ValueError("Invalid Wikimedia User-Agent")
        if http_policy is not None and any(value is not None for value in (max_retries, max_retry_wait, timeout)):
            raise ValueError("Pass an HTTP policy or legacy retry/timeout overrides, not both")
        if http_policy is None:
            overrides = {}
            if max_retries is not None:
                overrides["max_retries"] = max_retries
            if max_retry_wait is not None:
                overrides["max_retry_wait_seconds"] = max_retry_wait
            if timeout is not None:
                overrides["total_timeout_seconds"] = timeout
            http_policy = replace(settings.http_policy, **overrides) if overrides else settings.http_policy
        self.client = client
        self.http_policy = http_policy
        self.max_retries = http_policy.max_retries
        self.max_retry_wait = http_policy.max_retry_wait_seconds
        self.timeout = http_policy.total_timeout_seconds
        self.base_url = settings.source_endpoints[SOURCE]
        self._lock = asyncio.Lock()

    async def fetch_day(self, observation_date: date) -> tuple[DatasetSnapshot, list[PageObservation]]:
        async with self._lock:
            if self.client is not None:
                return await self._fetch(self.client, observation_date)
            async with httpx.AsyncClient(
                timeout=self.http_policy.request_timeout(), follow_redirects=False
            ) as client:
                return await self._fetch(client, observation_date)

    async def _fetch(self, client: httpx.AsyncClient, day: date) -> tuple[DatasetSnapshot, list[PageObservation]]:
        url = f"{self.base_url}/{day:%Y/%m/%d}"
        attempts = 0
        status_code = None
        error = None
        result = []
        count = None
        rejected = None
        rejected_records = ()
        status = FetchStatus.FETCH_ERROR
        while attempts <= self.max_retries:
            attempts += 1
            status_code = None
            try:
                response = await self.http_policy.get(
                    client,
                    url,
                    headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                )
            except (httpx.RequestError, HttpDeadlineExceeded) as exc:
                error = type(exc).__name__
                if attempts <= self.max_retries:
                    await asyncio.sleep(self.http_policy.backoff_delay(attempts - 1))
                    continue
                break
            status_code = response.status_code
            if status_code == 404:
                status, error = FetchStatus.DAY_UNAVAILABLE, "HTTP 404"
                break
            if status_code == 200:
                try:
                    fetched = datetime.now(timezone.utc)
                    result, count, rejected, rejected_records = _parse(response.json(), day, fetched)
                    status = (FetchStatus.PARTIAL if result else FetchStatus.FETCH_ERROR) if rejected else FetchStatus.SUCCESS
                    error = f"{rejected} invalid article records" if rejected else None
                except (ValueError, TypeError) as exc:
                    error = f"Invalid response: {exc}"
                break
            error = f"HTTP {status_code}"
            if self.http_policy.is_retryable_status(status_code) and attempts <= self.max_retries:
                delay = self.http_policy.retry_delay(
                    response.headers.get("Retry-After"), attempts - 1, datetime.now(timezone.utc)
                )
                if delay is None:
                    error += "; Retry-After exceeds wait limit"
                    break
                await asyncio.sleep(delay)
                continue
            break
        fetched_at = datetime.now(timezone.utc)
        # All rows from the response share a common capture timestamp.
        if result:
            fetched_at = result[0].fetched_at
        snapshot = DatasetSnapshot(source=SOURCE, country_scope="US", access="all-access", observation_date=day,
                                   fetched_at=fetched_at, fetch_status=status, http_status=status_code,
                                   item_count=count, request_url=url, request_count=attempts,
                                   rejected_count=rejected, error=error, rejected_records=rejected_records)
        return snapshot, result

    async def collect(self, start_date: date | None = None, *, max_lookback: int = 7, history_days: int = 7) -> CollectionResult:
        if not 0 <= max_lookback <= 30 or not 0 <= history_days <= 7:
            raise ValueError("Invalid lookback or history window")
        start_date = start_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
        days = []
        analysis_date = None
        for offset in range(max_lookback + 1):
            day = start_date - timedelta(days=offset)
            result = await self.fetch_day(day)
            days.append(result)
            if result[0].fetch_status in (FetchStatus.SUCCESS, FetchStatus.PARTIAL):
                analysis_date = day
                break
            if result[0].fetch_status != FetchStatus.DAY_UNAVAILABLE:
                break
        if analysis_date is not None:
            for offset in range(1, history_days + 1):
                days.append(await self.fetch_day(analysis_date - timedelta(days=offset)))
        return CollectionResult(analysis_date=analysis_date, days=tuple(days))
