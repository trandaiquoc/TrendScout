"""Wikimedia top-per-country adapter. No topic or language filter is applied."""

import asyncio
import os
import re
import math
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from app.models.observations import CollectionResult, DatasetSnapshot, FetchStatus, ObservationStatus, PageObservation
from app.sources.base import SourceMetadata

SOURCE = "wikimedia_pageviews_top_per_country"
BASE_URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/US/all-access"
DEFAULT_USER_AGENT = "TrendScout/0.1 (https://github.com/trandaiquoc/TrendScout)"
METADATA = SourceMetadata(
    source_id=SOURCE,
    name="Wikimedia Pageviews top per country",
    country_scope="US",
    access="all-access",
    metric="views_ceil",
    metric_description="Approximate upper-bound pageviews in the country top list",
    capabilities=("daily_snapshot", "latest_available", "seven_day_history", "top_list_censored"),
    configuration=("WIKIMEDIA_USER_AGENT", "max_retries", "max_retry_wait", "timeout"),
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
    if value:
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                return max(0.0, seconds)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.0, (parsed - now).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return float(2 ** attempt)


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
                 max_retries: int = 2, max_retry_wait: float = 30.0, timeout: float = 15.0):
        self.user_agent = user_agent or os.environ.get("WIKIMEDIA_USER_AGENT", DEFAULT_USER_AGENT)
        if not self.user_agent.strip() or "\n" in self.user_agent or "\r" in self.user_agent:
            raise ValueError("Invalid Wikimedia User-Agent")
        if not 0 <= max_retries <= 5 or not 0 <= max_retry_wait <= 300 or not 0 < timeout <= 60:
            raise ValueError("Invalid retry or timeout configuration")
        self.client = client
        self.max_retries = max_retries
        self.max_retry_wait = max_retry_wait
        self.timeout = timeout
        self._lock = asyncio.Lock()

    async def fetch_day(self, observation_date: date) -> tuple[DatasetSnapshot, list[PageObservation]]:
        async with self._lock:
            if self.client is not None:
                return await self._fetch(self.client, observation_date)
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                return await self._fetch(client, observation_date)

    async def _fetch(self, client: httpx.AsyncClient, day: date) -> tuple[DatasetSnapshot, list[PageObservation]]:
        url = f"{BASE_URL}/{day:%Y/%m/%d}"
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
                response = await client.get(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}, timeout=self.timeout)
            except httpx.RequestError as exc:
                error = type(exc).__name__
                if attempts <= self.max_retries:
                    await asyncio.sleep(min(2 ** (attempts - 1), self.max_retry_wait))
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
            if (status_code == 429 or 500 <= status_code <= 599) and attempts <= self.max_retries:
                delay = _retry_delay(response.headers.get("Retry-After"), attempts - 1, datetime.now(timezone.utc))
                if delay > self.max_retry_wait:
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
