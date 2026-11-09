"""Explainable Wikimedia pageview scoring; missing observations stay missing."""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
import math
from statistics import median

from app.adapters.wikimedia import SOURCE
from app.models.keywords import KeywordResult
from app.models.observations import DatasetSnapshot, FetchStatus, PageObservation

ALGORITHM_VERSION = "wikimedia-trend-v0.1"
WINDOW_DAYS = 7
MIN_BASELINE_DAYS = 3


@dataclass(frozen=True)
class DayData:
    snapshot: DatasetSnapshot
    keywords: tuple[KeywordResult, ...]
    # Validated adapter output before cleaning, so cleaning exclusions differ from absence.
    source_rows: tuple[PageObservation, ...]


@dataclass(frozen=True)
class TrendResult:
    keyword_id: str
    project: str
    article_original: str
    canonical_keyword: str
    categories: tuple[str, ...]
    source: str
    country_scope: str
    access: str
    observation_date: date
    current_views_ceil: int | None
    baseline: float | None
    ratio: float | None
    score: int | None
    status: str
    observed_baseline_days: int
    baseline_observations: tuple[tuple[date, int], ...]
    missing_baseline_dates: tuple[tuple[date, str], ...]
    score_components: dict[str, float | int] | None
    algorithm_version: str
    quality_flags: tuple[str, ...]
    evidence_url: str | None

    def to_record(self) -> dict:
        record = asdict(self)
        record["observation_date"] = self.observation_date.isoformat()
        record["baseline_observations"] = [{"date": day.isoformat(), "views_ceil": value} for day, value in self.baseline_observations]
        record["missing_baseline_dates"] = [{"date": day.isoformat(), "reason": reason} for day, reason in self.missing_baseline_dates]
        record["categories"] = list(self.categories)
        record["quality_flags"] = list(self.quality_flags)
        return record


def _valid_count(value: object) -> bool:
    # Upstream counts are integral; bool, NaN, infinity and fractional values are invalid.
    return type(value) is int and value >= 0


def _scope(row: KeywordResult | DatasetSnapshot) -> tuple[str, str, str]:
    return row.source, row.country_scope, row.access


def score_trends(analysis_date: date, days: list[DayData]) -> list[TrendResult]:
    snapshots = {}
    rows_by_key = defaultdict(list)
    raw_by_day = defaultdict(set)
    for day_data in days:
        snap = day_data.snapshot
        if snap.source != SOURCE:
            raise ValueError("Scoring profile only supports Wikimedia pageviews")
        key = (*_scope(snap), snap.observation_date)
        if key in snapshots:
            raise ValueError("Duplicate snapshot for scope and date")
        snapshots[key] = snap
        for raw in day_data.source_rows:
            if (raw.source, raw.country_scope, raw.access, raw.observation_date) != key:
                raise ValueError("Raw observation does not match snapshot scope/date")
            raw_by_day[key].add((raw.project, raw.article_original))
        for row in day_data.keywords:
            if (*_scope(row), row.observation_date) != key:
                raise ValueError("Keyword does not match snapshot scope/date")
            rows_by_key[(*key, row.project, row.article_original)].append(row)

    candidates = [r for day in days if day.snapshot.observation_date == analysis_date
                  and day.snapshot.fetch_status in (FetchStatus.SUCCESS, FetchStatus.PARTIAL) for r in day.keywords]
    counts = Counter((*_scope(r), r.project, r.article_original) for r in candidates)
    if any(count > 1 for count in counts.values()):
        raise ValueError("Duplicate current entity in snapshot")
    maxima = defaultdict(int)
    for row in candidates:
        if _valid_count(row.views_ceil):
            maxima[_scope(row)] = max(maxima[_scope(row)], row.views_ceil)

    output = []
    for current in candidates:
        scope = _scope(current)
        history = []
        missing = []
        flags = list(current.quality_flags) + ["CENSORED_TOP_LIST", "VIEWS_APPROXIMATE"]
        current_snapshot = snapshots[(*scope, analysis_date)]
        if current_snapshot.fetch_status == FetchStatus.PARTIAL:
            flags.append("CURRENT_SNAPSHOT_PARTIAL")
        for offset in range(1, WINDOW_DAYS + 1):
            day = analysis_date - timedelta(days=offset)
            day_key = (*scope, day)
            snap = snapshots.get(day_key)
            if snap is None:
                missing.append((day, "SNAPSHOT_MISSING"))
                continue
            if snap.fetch_status == FetchStatus.DAY_UNAVAILABLE:
                missing.append((day, "DAY_UNAVAILABLE"))
                continue
            if snap.fetch_status not in (FetchStatus.SUCCESS, FetchStatus.PARTIAL):
                missing.append((day, "FETCH_ERROR"))
                continue
            if snap.fetch_status == FetchStatus.PARTIAL:
                flags.append("HISTORY_SNAPSHOT_PARTIAL")
            matches = rows_by_key.get((*day_key, current.project, current.article_original), [])
            if len(matches) > 1:
                missing.append((day, "DUPLICATE_OBSERVATION"))
            elif matches:
                value = matches[0].views_ceil
                if _valid_count(value):
                    history.append((day, value))
                else:
                    missing.append((day, "INVALID_VALUE"))
            elif (current.project, current.article_original) in raw_by_day[day_key]:
                missing.append((day, "REJECTED_BY_CLEANING"))
            elif snap.fetch_status == FetchStatus.PARTIAL:
                missing.append((day, "PARTIAL_SNAPSHOT"))
            else:
                missing.append((day, "NOT_IN_TOP"))
        if missing:
            flags.append("INCOMPLETE_BASELINE_COVERAGE")
        base = median(value for _, value in history) if len(history) >= MIN_BASELINE_DAYS else None
        value = current.views_ceil
        ratio = None
        score = None
        components = None
        status = "INSUFFICIENT_DATA"
        maximum = maxima[scope]
        if _valid_count(value) and value > 0 and base is not None and base > 0 and maximum > 0:
            ratio = value / base
            growth = max(0.0, min(1.0, math.log2(ratio) / 3.0))
            volume = math.log1p(value) / math.log1p(maximum)
            score = max(0, min(100, round(100 * (0.7 * growth + 0.3 * volume))))
            components = {"growth": growth, "volume": volume, "max_views_ceil": maximum}
            status = "EMERGING" if ratio >= 1.5 and score >= 50 else "FALLING" if ratio < 0.75 else "STABLE"
        else:
            flags.append("INSUFFICIENT_DATA")
        output.append(TrendResult(keyword_id=current.keyword_id, project=current.project,
                    article_original=current.article_original, canonical_keyword=current.canonical_keyword,
                    categories=current.categories, source=current.source, country_scope=current.country_scope,
                    access=current.access, observation_date=analysis_date,
                    current_views_ceil=value if _valid_count(value) else None, baseline=base, ratio=ratio,
                    score=score, status=status, observed_baseline_days=len(history),
                    baseline_observations=tuple(history), missing_baseline_dates=tuple(missing),
                    score_components=components, algorithm_version=ALGORITHM_VERSION,
                    quality_flags=tuple(dict.fromkeys(flags)), evidence_url=current.source_url))
    return output
