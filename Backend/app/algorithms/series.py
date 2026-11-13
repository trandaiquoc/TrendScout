"""Reusable descriptive methods for complete, comparable numeric time series.

These methods deliberately reject the Wikimedia top-list feed: absence from that
feed is censored, so its observed rows cannot form an ordinary daily series.
"""
from datetime import date, timedelta
from statistics import mean, median

from app.algorithms.contracts import AlgorithmMetadata, AlgorithmResult
from app.algorithms.strategies import SOURCE, count_ok


def validate(item, minimum=3):
    if item.scope.source == SOURCE:
        return 'CENSORED_SOURCE_NOT_ACCEPTED'
    if not all((item.scope.source, item.scope.country, item.scope.metric, item.scope.unit)):
        return 'INVALID_SCOPE'
    if type(item.analysis_date) is not date or item.window_days not in (7, 14, 30):
        return 'UNSUPPORTED_WINDOW'
    if not count_ok(item.current_value):
        return 'INVALID_CURRENT'
    if item.current_snapshot_status != 'SUCCESS':
        return 'CURRENT_COVERAGE_UNCONFIRMED'
    points = sorted(item.history, key=lambda p: p.date)
    if len(points) < minimum:
        return 'INSUFFICIENT_SAMPLES'
    if len(points) != item.window_days:
        return 'MISSING_DAYS'
    expected = {item.analysis_date - timedelta(days=i) for i in range(item.window_days, 0, -1)}
    if {p.date for p in points} != expected:
        return 'MISSING_DAYS'
    if any(p.scope != item.scope for p in points):
        return 'INCOMPATIBLE_OBSERVATION'
    if any(p.status != 'OBSERVED' or p.snapshot_status != 'SUCCESS' or not count_ok(p.value)
           for p in points):
        return 'MISSING_OR_PARTIAL_OBSERVATION'
    return None


def _result(item, strategy, value, metric, unit, baseline, evidence, flags=()):
    return AlgorithmResult(item.keyword_id, strategy.metadata.algorithm_id,
        strategy.metadata.version, 'COMPLETED', value, metric, unit, item.current_value,
        baseline, item.window_days, len(item.history),
        (('history', tuple((p.date.isoformat(), p.value) for p in sorted(item.history, key=lambda p:p.date))),
         ('evidence_url', item.evidence_url), *evidence), flags,
        minimum_samples=strategy.metadata.minimum_samples,
        config_version=f'{strategy.metadata.algorithm_id}-config-v1')


class _SeriesAlgorithm:
    def applicable(self, item):
        return validate(item, self.metadata.minimum_samples)


class RollingMeanBaseline(_SeriesAlgorithm):
    metadata = AlgorithmMetadata('rolling-mean-baseline', 'v1', 'Rolling mean baseline', '*',
        'baseline_mean', 'input_unit', 3, 30, '*', '*',
        'Mean of the complete preceding daily window.', 'growth', ('daily_numeric_series',),
        (7, 14, 30), 'Requires every preceding day, with matching scope and unit.', False,
        (), 'O(n)', 'Descriptive arithmetic mean of prior observations.',
        ('Sensitive to spikes and level shifts.',))

    def execute(self, item):
        values = [p.value for p in item.history]
        baseline = mean(values)
        return _result(item, self, baseline, 'baseline_mean', item.scope.unit, baseline,
                       (('window_days', item.window_days),))


class RelativeChange(_SeriesAlgorithm):
    metadata = AlgorithmMetadata('relative-change', 'v1', 'Relative change', '*',
        'relative_change', 'dimensionless', 3, 30, '*', '*',
        'Current observation relative to the preceding-window mean.', 'growth',
        ('daily_numeric_series',), (7, 14, 30),
        'Requires complete preceding days and a positive baseline.', False, (), 'O(n)',
        '(current - mean baseline) / mean baseline.',
        ('Descriptive change, not a probability or causal effect.',))

    def applicable(self, item):
        reason = super().applicable(item)
        if reason:
            return reason
        if mean(p.value for p in item.history) <= 0:
            return 'ZERO_BASELINE'
        return None

    def execute(self, item):
        baseline = mean(p.value for p in item.history)
        value = (item.current_value - baseline) / baseline
        return _result(item, self, value, 'relative_change', 'dimensionless', baseline,
                       (('baseline_method', 'arithmetic_mean'),))


class TheilSenSlope(_SeriesAlgorithm):
    metadata = AlgorithmMetadata('theil-sen-slope', 'v1', 'Theil–Sen slope', '*',
        'theil_sen_slope', 'input_unit_per_day', 3, 30, '*', '*',
        'Median of all pairwise daily slopes, including the current observation.',
        'direction', ('daily_numeric_series',), (7, 14, 30),
        'Requires complete, equally spaced observations with matching scope and unit.',
        False, (), 'O(n²), capped at 31 observations.',
        'Robust descriptive change per day.',
        ('No inferential confidence interval is calculated.',))

    def execute(self, item):
        points = sorted((*item.history, _current_point(item)), key=lambda p:p.date)
        slopes = [(right.value - left.value) / (right.date - left.date).days
                  for i, left in enumerate(points) for right in points[i + 1:]]
        return _result(item, self, median(slopes), 'theil_sen_slope',
            f'{item.scope.unit}/day', None, (('pairwise_slope_count', len(slopes)),))


def _current_point(item):
    from app.algorithms.contracts import Observation
    return Observation(item.analysis_date, item.scope, 'OBSERVED', item.current_value)


class KendallTrend(_SeriesAlgorithm):
    metadata = AlgorithmMetadata('kendall-trend', 'v1', 'Kendall monotonic trend', '*',
        'kendall_tau', 'dimensionless', 3, 30, '*', '*',
        'Kendall tau-b across the preceding window and current observation.', 'direction',
        ('daily_numeric_series',), (7, 14, 30),
        'Requires complete, equally spaced observations with matching scope and unit.',
        False, (), 'O(n²), capped at 31 observations.',
        'Tau is descriptive rank concordance; positive indicates increasing values.',
        ('No significance test or p-value is calculated.',))

    def execute(self, item):
        values = [p.value for p in sorted((*item.history, _current_point(item)), key=lambda p:p.date)]
        concordant = discordant = ties_y = 0
        for i, left in enumerate(values):
            for right in values[i + 1:]:
                if right == left:
                    ties_y += 1
                else:
                    concordant += right > left
                    discordant += right < left
        n = len(values)
        denominator = ((n * (n - 1) // 2) *
                       (concordant + discordant + ties_y)) ** 0.5
        tau = (concordant - discordant) / denominator if denominator else 0.0
        return _result(item, self, tau, 'kendall_tau', 'dimensionless', None,
                       (('concordant_pairs', concordant), ('discordant_pairs', discordant),
                        ('tied_value_pairs', ties_y)))
