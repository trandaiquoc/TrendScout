"""Opt-in descriptive statistics; no inference on censored data is implied."""
from dataclasses import dataclass
from datetime import date, timedelta
from math import isfinite
from statistics import median

from app.algorithms.contracts import AlgorithmMetadata, AlgorithmResult
from app.algorithms.strategies import SOURCE, METRIC, UNIT, count_ok


@dataclass(frozen=True)
class RobustConfig:
    version: str = 'robust-mad-config-v0.1'
    minimum_samples: int = 14
    allow_top_censored: bool = False


@dataclass(frozen=True)
class EWMAConfig:
    version: str = 'ewma-config-v0.1'
    alpha: float = 0.3
    warmup: int = 7
    allow_top_censored: bool = False


def validate(item, minimum, censored):
    if item.scope.source != SOURCE: return 'UNSUPPORTED_SOURCE'
    if item.scope.metric != METRIC: return 'UNSUPPORTED_METRIC'
    if item.scope.unit != UNIT: return 'UNSUPPORTED_UNIT'
    if not all((item.scope.country, item.scope.access, item.scope.project, item.scope.article)):
        return 'INVALID_SCOPE'
    if type(item.analysis_date) is not date or type(item.window_days) is not int or not 1 <= item.window_days <= 90:
        return 'INVALID_WINDOW'
    if not count_ok(item.current_value): return 'INVALID_CURRENT'
    if item.current_snapshot_status != 'SUCCESS': return 'CURRENT_COVERAGE_UNCONFIRMED'
    dates = set()
    for point in item.history:
        if point.scope != item.scope: return 'INCOMPATIBLE_OBSERVATION'
        if type(point.date) is not date or not item.analysis_date - timedelta(days=item.window_days) <= point.date < item.analysis_date:
            return 'INVALID_WINDOW'
        if point.date in dates: return 'DUPLICATE_DATE'
        dates.add(point.date)
        if point.status != 'OBSERVED': return 'CENSORED_OR_MISSING'
        if point.snapshot_status != 'SUCCESS': return 'PARTIAL_COVERAGE'
        if not count_ok(point.value): return 'INVALID_OBSERVATION'
    if len(item.history) < minimum: return 'INSUFFICIENT_SAMPLES'
    if len(item.history) != item.window_days: return 'MISSING_DAYS'
    if not censored: return 'CENSORED_SOURCE_NOT_ACCEPTED'
    return None


def evidence(item, config):
    ordered = sorted(item.history, key=lambda x: str(x.date))
    return (('scope', (item.scope.source, item.scope.country, item.scope.access, item.scope.project, item.scope.article)),
            ('input_metric', item.scope.metric), ('input_unit', item.scope.unit),
            ('analysis_date', str(item.analysis_date)), ('window_days', item.window_days),
            ('historical_sample_count', len(item.history)), ('total_sample_count', len(item.history) + 1),
            ('historical_observations', tuple((str(p.date), p.status, p.value, p.snapshot_status) for p in ordered)),
            ('evidence_url', item.evidence_url), ('config', tuple(config)))


class RobustZMad:
    metadata = AlgorithmMetadata('robust-z-mad', 'v0.1', 'Robust Z / MAD', SOURCE,
        'robust_z', 'dimensionless', 14, 90, input_metric=METRIC, input_unit=UNIT)

    def __init__(self, config=RobustConfig()): self.config = config

    def applicable(self, item):
        cfg = self.config
        if type(cfg.minimum_samples) is not int or not 14 <= cfg.minimum_samples <= 90 or type(cfg.allow_top_censored) is not bool or cfg.version != 'robust-mad-config-v0.1':
            return 'INVALID_CONFIG'
        reason = validate(item, cfg.minimum_samples, cfg.allow_top_censored)
        if reason: return reason
        values = [p.value for p in item.history]
        center = median(values)
        if median(abs(x-center) for x in values) == 0: return 'ZERO_MAD'
        return None

    def skip_details(self, item, reason):
        detail = evidence(item, (('minimum_samples', self.config.minimum_samples), ('allow_top_censored', self.config.allow_top_censored)))
        if reason == 'ZERO_MAD':
            center = median(p.value for p in item.history)
            detail += (('historical_median', center), ('mad', 0))
        flags = ('CENSORED_TOP_LIST', 'VIEWS_APPROXIMATE') if item.scope.source == SOURCE else ()
        return detail, flags, self.config.minimum_samples, self.config.version

    def execute(self, item):
        values = [p.value for p in item.history]
        center = median(values)
        mad = median(abs(x-center) for x in values)
        z = 0.67448975 * (item.current_value-center) / mad
        ev = evidence(item, (('minimum_samples', self.config.minimum_samples), ('allow_top_censored', self.config.allow_top_censored)))
        return AlgorithmResult(item.keyword_id, self.metadata.algorithm_id, self.metadata.version, 'COMPLETED', z,
            'robust_z', 'dimensionless', item.current_value, center, item.window_days, len(values),
            ev + (('historical_median', center), ('mad', mad), ('scale', 0.67448975)),
            ('CENSORED_TOP_LIST', 'VIEWS_APPROXIMATE', 'CONDITIONAL_ON_TOP', 'NOT_SIGNIFICANCE_TEST'),
            minimum_samples=self.config.minimum_samples, config_version=self.config.version)


class EWMA:
    metadata = AlgorithmMetadata('ewma', 'v0.1', 'Daily EWMA', SOURCE,
        'ewma', UNIT, 7, 90, input_metric=METRIC, input_unit=UNIT)

    def __init__(self, config=EWMAConfig()): self.config = config

    def applicable(self, item):
        cfg = self.config
        if (not isinstance(cfg.alpha, (int, float)) or isinstance(cfg.alpha, bool) or not isfinite(cfg.alpha) or not 0 < cfg.alpha <= 1
            or type(cfg.warmup) is not int or not 7 <= cfg.warmup <= 90 or type(cfg.allow_top_censored) is not bool or cfg.version != 'ewma-config-v0.1'):
            return 'INVALID_CONFIG'
        return validate(item, cfg.warmup, cfg.allow_top_censored)

    def skip_details(self, item, reason):
        return (evidence(item, (('alpha', self.config.alpha), ('warmup', self.config.warmup),
                                ('initialization', 'first_historical_observation'), ('allow_top_censored', self.config.allow_top_censored))),
                ('CENSORED_TOP_LIST', 'VIEWS_APPROXIMATE') if item.scope.source == SOURCE else (), self.config.warmup, self.config.version)

    def execute(self, item):
        ordered = sorted(item.history, key=lambda p:p.date)
        reference = float(ordered[0].value)
        for point in ordered[1:]:
            reference = self.config.alpha * point.value + (1-self.config.alpha) * reference
        current = self.config.alpha * item.current_value + (1-self.config.alpha) * reference
        ev = evidence(item, (('alpha', self.config.alpha), ('warmup', self.config.warmup),
                             ('initialization', 'first_historical_observation'), ('cadence', '1_day'),
                             ('allow_top_censored', self.config.allow_top_censored)))
        return AlgorithmResult(item.keyword_id, self.metadata.algorithm_id, self.metadata.version, 'COMPLETED', current,
            'ewma', UNIT, item.current_value, reference, item.window_days, len(ordered),
            ev + (('historical_ewma', reference), ('current_ewma', current)),
            ('CENSORED_TOP_LIST', 'VIEWS_APPROXIMATE', 'CONDITIONAL_ON_TOP', 'NOT_SIGNIFICANCE_TEST'),
            minimum_samples=self.config.warmup, config_version=self.config.version)
