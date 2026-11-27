"""Snapshot-local popularity ranks; never imply uncensored population demand."""
from datetime import date

from app.algorithms.contracts import AlgorithmMetadata, AlgorithmResult
from app.algorithms.strategies import METRIC, SOURCE, UNIT, count_ok


class ObservedVolumeRanking:
    metadata = AlgorithmMetadata('observed-volume-rank', 'v4', 'Observed volume rank', SOURCE,
        'observed_volume_rank', 'ordinal_rank', 2, 1, METRIC, UNIT,
        'Rank a keyword by approximate volume within its persisted observed snapshot sample.',
        'popularity', ('daily_snapshot', 'top_list_censored'), (1,),
        'Requires two valid observations matching source, date, country, access, project, metric, and unit.',
        True, (), 'O(n log n) per snapshot cohort.',
        'Rank 1 is the largest observed value; ties receive the best tied rank.',
        ('Wikimedia top-list rank only; omitted pages are censored.', 'views_ceil is approximate.'))

    def applicable(self, item):
        if item.scope.source != SOURCE:
            return 'UNSUPPORTED_SOURCE'
        if item.scope.metric != METRIC:
            return 'UNSUPPORTED_METRIC'
        if item.scope.unit != UNIT:
            return 'UNSUPPORTED_UNIT'
        if (type(item.analysis_date) is not date
                or not all(isinstance(value, str) and value for value in
                           (item.scope.country, item.scope.access, item.scope.project, item.scope.article))):
            return 'INVALID_SCOPE'
        if item.current_snapshot_status not in {'SUCCESS', 'PARTIAL'}:
            return 'CURRENT_COVERAGE_UNCONFIRMED'
        if not count_ok(item.current_value):
            if item.current_value is None:
                return 'CURRENT_OBSERVATION_MISSING'
            return 'INVALID_CURRENT'
        values = self._values(item)
        if len(values) < 2:
            return 'INSUFFICIENT_COHORT'
        return None

    @staticmethod
    def _values(item):
        # Cohorts are intended to contain peers, but still reject self-inclusion,
        # duplicate pages, incompatible scopes, and observations from other dates.
        current_article = item.scope.article
        candidates = {}
        for point in item.cohort:
            if (point.status != 'OBSERVED'
                    or point.snapshot_status not in {'SUCCESS', 'PARTIAL'}
                    or point.date != item.analysis_date
                    or point.scope.source != item.scope.source
                    or point.scope.country != item.scope.country
                    or point.scope.access != item.scope.access
                    or point.scope.project != item.scope.project
                    or point.scope.metric != item.scope.metric
                    or point.scope.unit != item.scope.unit
                    or not point.scope.article
                    or not count_ok(point.value)):
                continue
            article = point.scope.article
            if article == current_article:
                continue
            candidates.setdefault(article, set()).add(point.value)
        # Exact duplicate rows collapse to one. Conflicting values for the same
        # page are excluded from this descriptive cohort rather than selected by
        # database iteration order.
        values = [item.current_value]
        values.extend(next(iter(candidates[key])) for key in sorted(candidates)
                      if len(candidates[key]) == 1)
        return values

    def execute(self, item):
        values = self._values(item)
        rank = 1 + sum(value > item.current_value for value in values)
        evidence = (('cohort_size', len(values)),
                    ('observation_date', item.analysis_date.isoformat()),
                    ('scope', (item.scope.source, item.scope.country, item.scope.access,
                               item.scope.project, item.scope.metric, item.scope.unit)),
                    ('evidence_url', item.evidence_url))
        return AlgorithmResult(item.keyword_id, self.metadata.algorithm_id, self.metadata.version,
            'COMPLETED', rank, self.metadata.metric, self.metadata.unit, item.current_value,
            None, 1, len(values), evidence,
            ('CENSORED_TOP_LIST', 'PARTIAL_SAMPLE', 'VIEWS_APPROXIMATE'),
            minimum_samples=2, config_version='observed-volume-rank-v4')

    def skip_details(self, item, reason):
        values = self._values(item) if count_ok(item.current_value) else []
        evidence = (('cohort_size', len(values)),
                    ('observation_date', item.analysis_date.isoformat()),
                    ('scope', (item.scope.source, item.scope.country, item.scope.access,
                               item.scope.project, item.scope.metric, item.scope.unit)),
                    ('evidence_url', item.evidence_url))
        flags = ('CENSORED_TOP_LIST', 'PARTIAL_SAMPLE', 'VIEWS_APPROXIMATE')
        return evidence, flags, 2, f'{self.metadata.algorithm_id}-{self.metadata.version}', len(values)


class ObservedPercentileRank(ObservedVolumeRanking):
    metadata = AlgorithmMetadata('observed-percentile-rank', 'v4', 'Observed percentile rank', SOURCE,
        'observed_percentile_rank', 'percentile_0_100', 2, 1, METRIC, UNIT,
        'Place approximate volume within this persisted snapshot’s observed sample.',
        'popularity', ('daily_snapshot', 'top_list_censored'), (1,),
        'Requires two valid observations matching source, date, country, access, project, metric, and unit.', True, (),
        'O(n log n) per snapshot cohort.',
        '100 is the largest observed value; tied values receive their midrank percentile.',
        ('Percentile is within the observed, censored sample only.', 'views_ceil is approximate.'))

    def execute(self, item):
        values = self._values(item)
        tied = sum(value == item.current_value for value in values)
        below = sum(value < item.current_value for value in values)
        percentile = 100 * (below + (tied - 1) / 2) / (len(values) - 1)
        evidence = (('cohort_size', len(values)), ('values_below', below), ('ties', tied),
                    ('observation_date', item.analysis_date.isoformat()),
                    ('scope', (item.scope.source, item.scope.country, item.scope.access,
                               item.scope.project, item.scope.metric, item.scope.unit)),
                    ('evidence_url', item.evidence_url))
        return AlgorithmResult(item.keyword_id, self.metadata.algorithm_id, self.metadata.version,
            'COMPLETED', percentile, self.metadata.metric, self.metadata.unit, item.current_value,
            None, 1, len(values), evidence,
            ('CENSORED_TOP_LIST', 'PARTIAL_SAMPLE', 'VIEWS_APPROXIMATE'),
            minimum_samples=2, config_version='observed-percentile-rank-v4')


class RelativeSourceRank:
    metadata = AlgorithmMetadata('relative-source-rank', 'v2', 'Relative source rank', '*',
        'relative_source_rank', 'percentile_0_100', 2, 1, 'result_rank',
        'ordinal result position',
        'Convert a provider’s query-scoped ordinal result position into a within-sample percentile.',
        'popularity', ('query_search',), (1,),
        'Requires at least two ranked records with matching provider and query scope.', False, (),
        'O(n log n) per source sample.', '100 is the best observed provider rank.',
        ('Only meaningful within a single provider query/sample.', 'Not search volume or demand.'),
        excluded_sources=('youtube_video_search',))

    def applicable(self, item):
        if item.scope.source == 'youtube_video_search':
            return 'POLICY_RESTRICTED'
        if item.scope.metric != 'result_rank' or item.scope.unit != 'ordinal result position':
            return 'UNSUPPORTED_METRIC_OR_UNIT'
        if item.scope.source == SOURCE:
            return 'UNSUPPORTED_SOURCE'
        if (type(item.analysis_date) is not date
                or not all(isinstance(value, str) and value for value in
                           (item.scope.source, item.scope.country, item.scope.access,
                            item.scope.project, item.scope.article))):
            return 'INVALID_SCOPE'
        if any((point.scope.source, point.scope.country, point.scope.access, point.scope.project,
                point.scope.metric, point.scope.unit) !=
               (item.scope.source, item.scope.country, item.scope.access, item.scope.project,
                item.scope.metric, item.scope.unit) for point in item.cohort):
            return 'INCOMPATIBLE_COHORT'
        if any(point.status != 'OBSERVED' or point.snapshot_status != 'SUCCESS'
               for point in item.cohort):
            return 'INCOMPATIBLE_COHORT'
        if type(item.current_value) is not int or item.current_value < 1:
            return 'INVALID_CURRENT_RANK'
        if len(self._peer_ranks(item)) + 1 < 2:
            return 'INSUFFICIENT_COHORT'
        return None

    @staticmethod
    def _peer_ranks(item):
        # Keep this method dormant until a persisted, provider/query-scoped
        # cohort exists. When called with a fixture, require stable item IDs,
        # exclude self-inclusion and collapse exact duplicate rows.
        peers = {}
        for point in item.cohort:
            if point.date != item.analysis_date:
                continue
            article = point.scope.article
            if not isinstance(article, str) or not article or article == item.scope.article:
                continue
            if type(point.rank) is not int or point.rank < 1:
                continue
            peers.setdefault(article, set()).add(point.rank)
        return [next(iter(peers[key])) for key in sorted(peers)
                if len(peers[key]) == 1]

    def execute(self, item):
        ranks = self._peer_ranks(item) + [item.current_value]
        better = sum(rank > item.current_value for rank in ranks)
        tied = sum(rank == item.current_value for rank in ranks)
        value = 100 * (better + (tied - 1) / 2) / (len(ranks) - 1)
        return AlgorithmResult(item.keyword_id, self.metadata.algorithm_id, self.metadata.version,
            'COMPLETED', value, self.metadata.metric, self.metadata.unit, item.current_value,
            None, 1, len(ranks), (('cohort_size', len(ranks)),
                ('observation_date', item.analysis_date.isoformat()),
                ('query_scope', (item.scope.source, item.scope.country, item.scope.access,
                                 item.scope.project)),
                ('evidence_url', item.evidence_url)),
            ('QUERY_SCOPED_SAMPLE',), minimum_samples=2, config_version='relative-source-rank-v2')
