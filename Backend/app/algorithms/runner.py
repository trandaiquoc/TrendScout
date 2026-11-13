"""Execute selected strategies independently; failure messages never expose input."""
from app.algorithms.contracts import AlgorithmResult


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
                    results.append(AlgorithmResult(item.keyword_id, meta.algorithm_id, meta.version,
                        'SKIPPED', None, meta.metric, meta.unit, item.current_value, None,
                        item.window_days, count, evidence, flags, reason,
                        'Input does not satisfy algorithm requirements', minimum_samples=minimum,
                        config_version=config_version))
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
