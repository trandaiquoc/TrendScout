"""Execute selected strategies independently; failure messages never expose input."""
from app.algorithms.contracts import AlgorithmResult


_INSUFFICIENT_REASONS = frozenset({
    'INSUFFICIENT_DATA', 'INSUFFICIENT_SAMPLES', 'INSUFFICIENT_COHORT',
    'MISSING_DAYS', 'MISSING_OR_PARTIAL_OBSERVATION', 'CENSORED_OR_MISSING',
    'PARTIAL_COVERAGE', 'CURRENT_COVERAGE_UNCONFIRMED',
    'CURRENT_OBSERVATION_MISSING', 'ZERO_BASELINE', 'ZERO_MAD',
})


def _skip_message(reason, *, item, minimum, count=None):
    if reason in {'INSUFFICIENT_DATA', 'INSUFFICIENT_SAMPLES'}:
        available = count if count is not None else sum(
            point.status == 'OBSERVED' for point in item.history
        )
        return f'Only {available} compatible historical observations are available; this method requires {minimum}.'
    if reason == 'INSUFFICIENT_COHORT':
        available = count if count is not None else 0
        return f'Only {available} comparable records are available in the source snapshot or query cohort.'
    if reason == 'MISSING_DAYS':
        return f'Complete daily history for the selected {item.window_days}-day window is unavailable.'
    if reason in {'MISSING_OR_PARTIAL_OBSERVATION', 'CENSORED_OR_MISSING', 'PARTIAL_COVERAGE'}:
        return 'The required historical observations are missing, censored, or partial.'
    if reason == 'CURRENT_COVERAGE_UNCONFIRMED':
        return 'The current source snapshot is partial or its coverage could not be confirmed.'
    if reason == 'CURRENT_OBSERVATION_MISSING':
        return 'There is no current observation for this keyword in the selected source snapshot.'
    if reason == 'POLICY_RESTRICTED':
        return 'Provider policy excludes this source from algorithmic derivation.'
    if reason == 'CENSORED_SOURCE_NOT_ACCEPTED':
        return 'This method requires a complete daily series and cannot use the censored top-list source.'
    if reason == 'ZERO_BASELINE':
        return 'The historical baseline is zero, so this ratio or change is undefined.'
    if reason == 'ZERO_MAD':
        return 'Historical values have zero median absolute deviation, so a robust Z value is undefined.'
    return "Input does not satisfy this algorithm's source, metric, scope, or configuration requirements."


class AlgorithmRunner:
    def __init__(self, registry):
        self.registry = registry

    def run(self, item, selected):
        keys = sorted(set(selected))
        strategies = [self.registry.get(key) for key in keys]  # fail before execution
        results = []
        for strategy in strategies:
            meta = strategy.metadata
            try:
                reason = strategy.applicable(item)
                if reason:
                    history = tuple((str(p.date), p.status) for p in item.history)
                    count = sum(p.status == 'OBSERVED' for p in item.history)
                    flags = ()
                    if item.scope.source == 'wikimedia_pageviews_top_per_country':
                        flags = ('CENSORED_TOP_LIST', 'VIEWS_APPROXIMATE')
                    if item.legacy_result is not None and meta.algorithm_id == 'wikimedia-trend':
                        count = item.legacy_result.observed_baseline_days
                        flags = item.legacy_result.quality_flags
                        history = tuple((str(day), status) for day, status in item.legacy_result.missing_baseline_dates)
                    evidence = (('coverage', history), ('evidence_url', item.evidence_url))
                    minimum = item.minimum_samples
                    config_version = 'algorithm-input-v0.1'
                    if callable(getattr(type(strategy), 'skip_details', None)):
                        details = strategy.skip_details(item, reason)
                        evidence, flags, minimum, config_version = details[:4]
                        if len(details) > 4:
                            count = details[4]
                    status = 'INSUFFICIENT_DATA' if reason in _INSUFFICIENT_REASONS else 'SKIPPED'
                    results.append(AlgorithmResult(item.keyword_id, meta.algorithm_id, meta.version,
                        status, None, meta.metric, meta.unit, item.current_value, None,
                        item.window_days, count, evidence, flags, reason,
                        _skip_message(reason, item=item, minimum=minimum, count=count), minimum_samples=minimum,
                        config_version=config_version or meta.version))
                else:
                    results.append(strategy.execute(item))
            except Exception:
                results.append(AlgorithmResult(item.keyword_id, meta.algorithm_id, meta.version,
                    'FAILED', None, meta.metric, meta.unit, item.current_value, None,
                    item.window_days, 0, (), (), 'STRATEGY_ERROR', 'Algorithm execution failed',
                    minimum_samples=item.minimum_samples))
        return tuple(results)

    def legacy_results(self, analysis_date, days):
        """Compatibility path: one batch scorer call, preserving existing failure semantics."""
        return self.registry.get('wikimedia-trend@v0.1').score_batch(analysis_date, days)
