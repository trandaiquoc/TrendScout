"""Wikimedia-specific legacy, ratio and baseline strategies."""
from datetime import date, timedelta
from statistics import median

from app.algorithms.contracts import AlgorithmMetadata, AlgorithmResult

SOURCE = 'wikimedia_pageviews_top_per_country'
METRIC = 'views_ceil'
UNIT = 'approx_pageviews_upper_bound'
KNOWN_MISSING = {'NOT_IN_TOP','DAY_UNAVAILABLE','FETCH_ERROR','SNAPSHOT_MISSING','PARTIAL_SNAPSHOT','REJECTED_BY_CLEANING'}


def count_ok(value):
    return type(value) is int and value >= 0


def applicability(item, *, needs_current):
    if item.scope.source != SOURCE: return 'UNSUPPORTED_SOURCE'
    if item.scope.metric != METRIC: return 'UNSUPPORTED_METRIC'
    if item.scope.unit != UNIT: return 'UNSUPPORTED_UNIT'
    if not all((item.scope.country, item.scope.access, item.scope.project, item.scope.article)): return 'INVALID_SCOPE'
    if type(item.analysis_date) is not date or type(item.window_days) is not int or not 1 <= item.window_days <= 7:
        return 'INVALID_WINDOW'
    if type(item.minimum_samples) is not int or not 3 <= item.minimum_samples <= item.window_days:
        return 'INVALID_CONFIG'
    if needs_current and not count_ok(item.current_value): return 'INVALID_CURRENT'
    dates = set()
    for point in item.history:
        if point.scope != item.scope: return 'INCOMPATIBLE_OBSERVATION'
        if type(point.date) is not date or not item.analysis_date - timedelta(days=item.window_days) <= point.date < item.analysis_date:
            return 'INVALID_WINDOW'
        if point.date in dates: return 'DUPLICATE_DATE'
        dates.add(point.date)
        if point.status == 'OBSERVED':
            if point.snapshot_status not in ('SUCCESS', 'PARTIAL'): return 'INVALID_COVERAGE'
            if not count_ok(point.value): return 'INVALID_OBSERVATION'
        elif point.status not in KNOWN_MISSING or point.value is not None:
            return 'INVALID_OBSERVATION'
    if sum(point.status == 'OBSERVED' for point in item.history) < item.minimum_samples:
        return 'INSUFFICIENT_SAMPLES'
    return None


def result(item, meta, value, baseline, samples):
    history = tuple((p.date.isoformat(), p.value) for p in sorted(item.history, key=lambda x: x.date, reverse=True) if p.status == 'OBSERVED')
    missing = tuple((p.date.isoformat(), p.status) for p in sorted(item.history, key=lambda x: x.date, reverse=True) if p.status != 'OBSERVED')
    covered = {p.date for p in item.history}
    missing += tuple(((item.analysis_date - timedelta(days=i)).isoformat(), 'SNAPSHOT_MISSING')
                     for i in range(1, item.window_days + 1) if item.analysis_date - timedelta(days=i) not in covered)
    missing = tuple(sorted(missing, reverse=True))
    flags = ('CENSORED_TOP_LIST', 'VIEWS_APPROXIMATE') + (('INCOMPLETE_BASELINE_COVERAGE',) if missing else ())
    if any(p.status == 'OBSERVED' and p.snapshot_status == 'PARTIAL' for p in item.history):
        flags += ('HISTORY_SNAPSHOT_PARTIAL',)
    return AlgorithmResult(item.keyword_id, meta.algorithm_id, meta.version, 'COMPLETED', value,
        meta.metric, meta.unit, item.current_value, baseline, item.window_days, len(samples),
        (('observations', history), ('missing', missing), ('evidence_url', item.evidence_url)), flags,
        minimum_samples=item.minimum_samples)


class MedianBaseline:
    metadata = AlgorithmMetadata('median-baseline','v0.1','Median baseline',SOURCE,METRIC,UNIT,3,7,input_metric=METRIC,input_unit=UNIT)
    def applicable(self, item): return applicability(item, needs_current=False)
    def execute(self, item):
        samples = [p.value for p in item.history if p.status == 'OBSERVED']
        base = median(samples)
        return result(item, self.metadata, base, base, samples)


class GrowthRatio:
    metadata = AlgorithmMetadata('growth-ratio','v0.1','Current / median baseline',SOURCE,'growth_ratio','dimensionless',3,7,input_metric=METRIC,input_unit=UNIT)
    def applicable(self, item):
        reason = applicability(item, needs_current=True)
        if reason: return reason
        if median(p.value for p in item.history if p.status == 'OBSERVED') <= 0: return 'ZERO_BASELINE'
        return None
    def execute(self, item):
        samples = [p.value for p in item.history if p.status == 'OBSERVED']
        base = median(samples)
        return result(item, self.metadata, item.current_value / base, base, samples)


class WikimediaLegacy:
    metadata = AlgorithmMetadata('wikimedia-trend','v0.1','Legacy Wikimedia trend score',SOURCE,'trend_score','points_0_100',3,7,input_metric=METRIC,input_unit=UNIT)
    def __init__(self, scorer): self.scorer = scorer
    def score_batch(self, analysis_date, days): return self.scorer(analysis_date, days)
    def applicable(self, item):
        if item.scope.source != SOURCE: return 'UNSUPPORTED_SOURCE'
        if item.scope.metric != METRIC: return 'UNSUPPORTED_METRIC'
        if item.scope.unit != UNIT: return 'UNSUPPORTED_UNIT'
        if item.window_days != 7 or item.minimum_samples != 3 or type(item.analysis_date) is not date: return 'INVALID_CONFIG'
        if item.legacy_result is None: return 'LEGACY_RESULT_REQUIRED'
        legacy = item.legacy_result
        if (legacy.keyword_id != item.keyword_id or legacy.source != item.scope.source or
            legacy.country_scope != item.scope.country or legacy.access != item.scope.access or
            legacy.project != item.scope.project or legacy.article_original != item.scope.article or
            legacy.observation_date != item.analysis_date or legacy.current_views_ceil != item.current_value): return 'INCOMPATIBLE_OBSERVATION'
        if legacy.algorithm_version != 'wikimedia-trend-v0.1': return 'UNSUPPORTED_ALGORITHM_VERSION'
        if legacy.status == 'INSUFFICIENT_DATA': return 'INSUFFICIENT_DATA'
        return None
    def execute(self, item):
        legacy = item.legacy_result
        return AlgorithmResult(item.keyword_id, self.metadata.algorithm_id, self.metadata.version,
            'COMPLETED', legacy.score, self.metadata.metric, self.metadata.unit, legacy.current_views_ceil,
            legacy.baseline, item.window_days, legacy.observed_baseline_days,
            (('legacy_status', legacy.status), ('evidence_url', legacy.evidence_url),
             ('baseline_observations', legacy.baseline_observations), ('missing', legacy.missing_baseline_dates)),
            legacy.quality_flags, minimum_samples=item.minimum_samples)
