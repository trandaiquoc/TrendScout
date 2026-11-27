"""Synthetic algorithm contracts; no provider access."""
import subprocess
import sys
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import Mock

from test_golden_contract import build_golden

from app.algorithms.catalog import PersistedInputAdapter, catalog, persisted_keys_for_source
from app.algorithms.catalog import registry as algorithm_registry
from app.algorithms.contracts import AlgorithmInput, AlgorithmMetadata, Observation, Scope
from app.algorithms.google_trends import INCREASE_METRIC, INCREASE_UNIT
from app.algorithms.registry import AlgorithmRegistry
from app.algorithms.runner import AlgorithmRunner
from app.algorithms.strategies import (
    METRIC,
    SOURCE,
    UNIT,
    MedianBaseline,
    WikimediaLegacy,
)
from app.composition import algorithm_engine
from app.sources.catalog import GOOGLE_SOURCE_ID, SOURCE_CAPABILITIES

DAY = date(2026,1,8)
SCOPE = Scope(SOURCE,'US','all-access','en.wikipedia','Example',METRIC,UNIT)


def make_input(values=(100,200,300), current=800):
    history = tuple(Observation(DAY-timedelta(days=i+1),SCOPE,'OBSERVED',v) for i,v in enumerate(values))
    return AlgorithmInput('synthetic-id', SCOPE, DAY, current, history, evidence_url='https://en.wikipedia.org/wiki/Example',
                          current_snapshot_status='SUCCESS')


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.runner = algorithm_engine()
        self.keys = ('growth-ratio@v0.1','median-baseline@v0.1')

    def test_import_has_no_network_side_effect(self):
        script = """
import socket
from unittest.mock import patch
with patch.object(socket.socket, 'connect', side_effect=AssertionError('network')):
    import app.algorithms.contracts, app.algorithms.registry, app.algorithms.runner, app.algorithms.strategies
"""
        run = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_registry_duplicate_unknown_selection_and_no_implicit_execution(self):
        registry = AlgorithmRegistry()
        strategy = Mock()
        strategy.metadata = AlgorithmMetadata('fake','v1','Fake',SOURCE,METRIC,UNIT,3,7)
        registry.register(strategy)
        strategy.execute.assert_not_called()
        with self.assertRaises(ValueError):
            registry.register(strategy)
        with self.assertRaises(ValueError):
            AlgorithmRunner(registry).run(make_input(), ['unknown@v1'])
        self.assertEqual(AlgorithmRunner(registry).run(make_input(), []), ())
        strategy.applicable.assert_not_called()

    def test_selected_only_and_registered_metadata(self):
        registry = AlgorithmRegistry()
        first, second = Mock(), Mock()
        first.metadata = AlgorithmMetadata('first','v1','First',SOURCE,METRIC,UNIT,3,7)
        second.metadata = AlgorithmMetadata('second','v1','Second',SOURCE,METRIC,UNIT,3,7)
        first.applicable.return_value = 'INSUFFICIENT_SAMPLES'
        registry.register(first)
        registry.register(second)
        output = AlgorithmRunner(registry).run(make_input(), ['first@v1'])
        self.assertEqual(output[0].status,'INSUFFICIENT_DATA')
        first.execute.assert_not_called()
        second.applicable.assert_not_called()
        second.execute.assert_not_called()
        self.assertEqual(self.runner.registry.get('growth-ratio@v0.1').metadata.input_metric, METRIC)
        self.assertEqual(self.runner.registry.get('growth-ratio@v0.1').metadata.input_unit, UNIT)

    def test_odd_even_growth_and_independent_units(self):
        odd = self.runner.run(make_input(), self.keys)
        self.assertEqual([x.algorithm_id for x in odd], ['growth-ratio','median-baseline'])
        self.assertEqual((odd[0].status,odd[0].value,odd[0].baseline_value,odd[0].sample_count), ('COMPLETED',4,200,3))
        self.assertEqual((odd[1].value,odd[1].metric_unit), (200,UNIT))
        self.assertEqual(odd[0].metric_unit, 'dimensionless')
        self.assertEqual(len(dict(odd[0].evidence)['observation_dates']), 3)
        self.assertIn('CENSORED_TOP_LIST', odd[0].quality_flags)
        self.assertIn('INCOMPLETE_BASELINE_COVERAGE', odd[0].quality_flags)
        even = self.runner.run(make_input((100,200,300,400)), self.keys)
        self.assertEqual((even[1].value,even[0].value), (250,3.2))
        self.assertEqual(self.runner.run(make_input(current=0), self.keys)[0].value, 0)

    def test_skips_invalid_input_without_inventing_zero(self):
        base = make_input()
        cases = [(replace(base, scope=replace(SCOPE,source='reddit')), 'UNSUPPORTED_SOURCE'),
                 (replace(base, scope=replace(SCOPE,metric='likes')), 'UNSUPPORTED_METRIC'),
                 (replace(base, scope=replace(SCOPE,unit='other')), 'UNSUPPORTED_UNIT'),
                 (replace(base, window_days=8), 'INVALID_WINDOW'),
                 (replace(base, minimum_samples=2), 'INVALID_CONFIG'),
                 (replace(base, history=base.history[:2]), 'INSUFFICIENT_SAMPLES'),
                 (replace(base, current_value=None), 'CURRENT_OBSERVATION_MISSING'),
                 (replace(base, history=tuple(replace(x,value=0) for x in base.history)), 'ZERO_BASELINE'),
                 (replace(base, history=(replace(base.history[0],scope=replace(SCOPE,project='fr.wikipedia')),)+base.history[1:]), 'INCOMPATIBLE_OBSERVATION'),
                 (replace(base, history=(replace(base.history[0],value=None),)+base.history[1:]), 'INVALID_OBSERVATION')]
        for item, code in cases:
            with self.subTest(code=code):
                result = self.runner.run(item, ['growth-ratio@v0.1'])[0]
                status = 'INSUFFICIENT_DATA' if code in {
                    'INSUFFICIENT_SAMPLES', 'CURRENT_OBSERVATION_MISSING', 'ZERO_BASELINE',
                } else 'SKIPPED'
                self.assertEqual((result.status,result.reason_code,result.value), (status,code,None))
        zero = self.runner.run(make_input((0,0,0)), ['median-baseline@v0.1'])[0]
        self.assertEqual((zero.status,zero.value),('COMPLETED',0))

    def test_censored_is_missing_and_not_zero(self):
        base = make_input()
        missing = Observation(DAY-timedelta(days=4),SCOPE,'NOT_IN_TOP',None)
        unavailable = Observation(DAY-timedelta(days=5),SCOPE,'DAY_UNAVAILABLE',None)
        enriched = replace(base, history=base.history+(missing,unavailable))
        before = deepcopy(enriched)
        result = self.runner.run(enriched, self.keys)
        self.assertEqual(enriched,before)
        reversed_input = replace(enriched, history=tuple(reversed(enriched.history)))
        self.assertEqual(result, self.runner.run(reversed_input, self.keys))
        partial = replace(enriched, history=(replace(enriched.history[0], snapshot_status='PARTIAL'),) + enriched.history[1:])
        self.assertIn('HISTORY_SNAPSHOT_PARTIAL', self.runner.run(partial, self.keys)[0].quality_flags)
        self.assertEqual(result[0].sample_count,3)
        self.assertIn(('2026-01-04','NOT_IN_TOP'),dict(result[0].evidence)['missing'])
        invalid = replace(enriched,history=base.history+(replace(missing,value=0),))
        self.assertEqual(self.runner.run(invalid,self.keys)[0].reason_code,'INVALID_OBSERVATION')

    def test_runner_isolates_failure_and_never_leaks_exception(self):
        registry = AlgorithmRegistry()
        bad = Mock()
        bad.metadata = AlgorithmMetadata('bad','v1','Bad',SOURCE,METRIC,UNIT,3,7)
        bad.applicable.return_value = None
        bad.execute.side_effect = RuntimeError('sensitive synthetic secret')
        registry.register(bad)
        registry.register(MedianBaseline())
        results = AlgorithmRunner(registry).run(make_input(), ['median-baseline@v0.1','bad@v1'])
        self.assertEqual([x.status for x in results],['FAILED','COMPLETED'])
        self.assertEqual(results[0].reason_message,'Algorithm execution failed')
        self.assertNotIn('sensitive', repr(results))
        self.assertIsNone(results[0].value)

    def test_legacy_batch_calls_once_and_preserves_original_exception(self):
        scorer = Mock(return_value=['unchanged'])
        registry = AlgorithmRegistry()
        registry.register(WikimediaLegacy(scorer))
        runner = AlgorithmRunner(registry)
        self.assertEqual(runner.legacy_results(DAY, []), ['unchanged'])
        scorer.assert_called_once_with(DAY, [])
        scorer.side_effect = ValueError('invalid shared data')
        with self.assertRaises(ValueError):
            runner.legacy_results(DAY, [])

    def test_reusable_series_methods_execute_only_on_complete_uncensored_history(self):
        scope = Scope('provider_daily_series', 'US', 'public', 'search', 'robot vacuum', 'observed_count', 'items')
        history = tuple(Observation(DAY-timedelta(days=i), scope, 'OBSERVED', 100 + (7-i)*10)
                        for i in range(7, 0, -1))
        item = AlgorithmInput('keyword', scope, DAY, 180, history, window_days=7,
                              minimum_samples=3, current_snapshot_status='SUCCESS')
        selected = ['rolling-mean-baseline@v1', 'relative-change@v1',
                    'theil-sen-slope@v1', 'kendall-trend@v2']
        results = self.runner.run(item, selected)
        self.assertEqual([result.status for result in results], ['COMPLETED'] * 4)
        values = {result.algorithm_id: result.value for result in results}
        self.assertEqual(values['rolling-mean-baseline'], 130)
        self.assertAlmostEqual(values['relative-change'], 5/13)
        self.assertEqual(values['theil-sen-slope'], 10)
        self.assertEqual(values['kendall-trend'], 1)
        censored = replace(item, scope=SCOPE)
        skipped = self.runner.run(censored, selected)
        self.assertEqual({result.reason_code for result in skipped}, {'CENSORED_SOURCE_NOT_ACCEPTED'})

    def test_robust_and_ewma_methods_accept_complete_uncensored_source_series(self):
        scope = Scope('provider_daily_series', 'US', 'public', 'search', 'robot vacuum',
                      'observed_count', 'items')
        history = tuple(Observation(DAY - timedelta(days=i), scope, 'OBSERVED', 100 + (14 - i) * 10)
                        for i in range(14, 0, -1))
        item = AlgorithmInput('keyword', scope, DAY, 240, history, window_days=14,
                              minimum_samples=14, current_snapshot_status='SUCCESS')
        results = self.runner.run(item, ['robust-z-mad@v0.2', 'ewma@v0.2'])
        self.assertEqual([result.status for result in results], ['COMPLETED', 'COMPLETED'])
        self.assertTrue(all('CENSORED_TOP_LIST' not in result.quality_flags for result in results))
        by_algorithm = {result.algorithm_id: result for result in results}
        self.assertEqual((by_algorithm['ewma'].metric_name, by_algorithm['ewma'].metric_unit),
                         ('ewma', 'items'))

    def test_kendall_trend_uses_tau_b_when_values_tie(self):
        scope = Scope('provider_daily_series', 'US', 'public', 'search', 'robot vacuum',
                      'observed_count', 'items')
        values = (1, 1, 3, 3, 5, 5, 7)
        history = tuple(Observation(DAY - timedelta(days=7 - index), scope, 'OBSERVED', value)
                        for index, value in enumerate(values))
        item = AlgorithmInput('keyword', scope, DAY, 7, history, window_days=7,
                              minimum_samples=3, current_snapshot_status='SUCCESS')
        result = self.runner.run(item, ['kendall-trend@v2'])[0]
        self.assertEqual(result.status, 'COMPLETED')
        # Seven history points plus current produce 28 pairs: 24 concordant,
        # four tied in value, and none tied in time (tau-b = 24/sqrt(28*24)).
        self.assertAlmostEqual(result.value, (6 / 7) ** 0.5)

    def test_google_trends_increase_rank_uses_only_same_snapshot_provider_signal(self):
        scope = Scope(GOOGLE_SOURCE_ID, 'US_FILTER_ONLY', 'en',
                      'google_trends_us_en_24h', 'keyword-a',
                      INCREASE_METRIC, INCREASE_UNIT)
        cohort = (
            Observation(DAY, replace(scope, article='keyword-b'), 'OBSERVED', 120.5, 'SUCCESS'),
            Observation(DAY, replace(scope, article='keyword-c'), 'OBSERVED', 110.0, 'SUCCESS'),
            Observation(DAY, replace(scope, article='keyword-d'), 'OBSERVED', 80.0, 'SUCCESS'),
        )
        item = AlgorithmInput(
            'keyword-a', scope, DAY, 110.0, (), window_days=1, minimum_samples=2,
            current_snapshot_status='SUCCESS', cohort=cohort,
        )
        result = self.runner.run(item, ['google-trends-increase-rank@v1'])[0]
        self.assertEqual((result.status, result.value, result.sample_count), ('COMPLETED', 2, 4))
        self.assertEqual((result.metric_name, result.metric_unit),
                         ('provider_increase_rank', 'ordinal_rank'))
        self.assertIn('SINGLE_SNAPSHOT_COHORT', result.quality_flags)

        partial = replace(item, current_snapshot_status='PARTIAL')
        skipped = self.runner.run(partial, ['google-trends-increase-rank@v1'])[0]
        self.assertEqual((skipped.status, skipped.reason_code, skipped.value),
                         ('INSUFFICIENT_DATA', 'CURRENT_COVERAGE_UNCONFIRMED', None))

    def test_registry_catalog_is_metadata_driven_and_marks_unavailable_methods(self):
        response = catalog()
        ids = {item['algorithm_id'] for item in response['algorithms']}
        self.assertTrue({'wikimedia-trend', 'growth-ratio', 'median-baseline',
                         'robust-z-mad', 'ewma', 'theil-sen-slope', 'kendall-trend'} <= ids)
        execution = {item['algorithm_id']: item['execution']['status']
                     for item in response['algorithms']}
        self.assertEqual({key for key, status in execution.items()
                          if status == 'PERSISTED_SOURCE_ADAPTER'},
                         {'growth-ratio', 'median-baseline', 'observed-volume-rank',
                          'observed-percentile-rank', 'google-trends-increase-rank'})
        self.assertEqual(len(execution), 13)
        self.assertEqual(execution['wikimedia-trend'], 'LEGACY_PATH_ONLY')
        self.assertEqual(execution['robust-z-mad'], 'NOT_EXECUTED_SOURCE_AND_RETENTION')
        self.assertEqual(execution['relative-source-rank'], 'REGISTERED_NO_QUERY_COHORT')
        growth = next(item for item in response['algorithms']
                      if item['algorithm_id'] == 'growth-ratio')
        self.assertEqual(growth['execution']['sources'], (SOURCE,))
        self.assertEqual(growth['execution']['input_adapters'],
                         ('wikimedia_pageviews_observation_projection',))
        trends = next(item for item in response['algorithms']
                      if item['algorithm_id'] == 'google-trends-increase-rank')
        self.assertEqual(trends['execution']['input_profiles'][0]['profile_id'],
                         'google_trends_us_en_24h')
        self.assertIn('valid source ProcessingGrant',
                      trends['execution']['policy_requirements'][0])
        trend_profile = next(
            source for source in trends['sources']
            if source.get('source_profile_id') == 'google_trends_us_en_24h'
        )
        self.assertEqual(trend_profile['status'], 'SUPPORTED_BUT_POLICY_GATED')
        self.assertEqual(trend_profile['metric'], 'provider_increase_percentage')
        relative = next(item for item in response['algorithms']
                        if item['algorithm_id'] == 'relative-source-rank')
        self.assertEqual(relative['execution']['sources'], ())
        for source_id in ('google_web_serp', 'bing_web_serp'):
            source = next(item for item in relative['sources']
                          if item['source_id'] == source_id)
            self.assertEqual((source['status'], source['reason']),
                             ('SUPPORTED_BUT_NOT_LIVE', 'PENDING_APPROVAL'))
        theil = next(item for item in response['algorithms'] if item['algorithm_id'] == 'theil-sen-slope')
        wikimedia = next(source for source in theil['sources'] if source['source_id'] == SOURCE)
        self.assertEqual(wikimedia['status'], 'INCOMPATIBLE')
        relative = next(item for item in response['algorithms'] if item['algorithm_id'] == 'relative-source-rank')
        youtube = next(source for source in relative['sources'] if source['source_id'] == 'youtube_video_search')
        self.assertEqual((youtube['status'], youtube['reason']), ('INCOMPATIBLE', 'POLICY_RESTRICTED'))
        self.assertEqual(response['evaluation_status'], 'NOT_YET_EVALUATED')

    def test_catalog_uses_a_mock_second_source_without_provider_calls(self):
        wikimedia = SOURCE_CAPABILITIES[0]
        mock_source = replace(
            wikimedia,
            source_id='mock_second_source', provider_id='mock_adapter',
            display_name='Mock source', icon='M', readiness='PENDING_APPROVAL',
            default_enabled=False, adapter_status='IMPLEMENTED_OFFLINE_VERIFIED',
            approval_status='PENDING_APPROVAL', supported_data_modes=('SYNTHETIC',),
            capabilities=('query_search',), metric_name='result_rank',
            metric_unit='ordinal result position',
            local_live_verification_status='NOT_VERIFIED', production_readiness='NOT_APPROVED',
        )
        response = catalog((mock_source,))
        relative = next(item for item in response['algorithms']
                        if item['algorithm_id'] == 'relative-source-rank')
        capability = relative['sources'][0]
        self.assertEqual(capability['source_id'], 'mock_second_source')
        self.assertEqual(capability['status'], 'SUPPORTED_BUT_NOT_LIVE')
        self.assertEqual((capability['icon'], capability['display_name']), ('M', 'Mock source'))
        daily_source = replace(
            wikimedia,
            source_id='mock_daily_series', provider_id='mock_series_adapter',
            display_name='Mock daily series', icon='D', readiness='PENDING_APPROVAL',
            default_enabled=False, adapter_status='IMPLEMENTED_OFFLINE_VERIFIED',
            approval_status='PENDING_APPROVAL', supported_data_modes=('SYNTHETIC',),
            capabilities=('daily_numeric_series',), metric_name='observed_count',
            metric_unit='items',
            local_live_verification_status='NOT_VERIFIED', production_readiness='NOT_APPROVED',
        )
        daily_catalog = catalog((daily_source,))
        robust = next(item for item in daily_catalog['algorithms']
                      if item['algorithm_id'] == 'robust-z-mad')
        self.assertEqual(robust['sources'][0]['status'], 'SUPPORTED_BUT_NOT_LIVE')

    def test_persisted_dispatch_selects_algorithms_from_source_adapter_contracts(self):
        registered = algorithm_registry()
        daily = PersistedInputAdapter(
            adapter_id='mock_daily_observation_projection',
            source_id='mock_daily_series', metric='observed_count', unit='items',
            source_capabilities=('daily_numeric_series',),
            runtime_capabilities=('daily_observations', 'observation_history',
                                  'input_digest', 'evidence_lineage'),
            max_history_days=30,
        )
        daily_keys = persisted_keys_for_source(
            registered, 'mock_daily_series', input_adapters=(daily,),
        )
        self.assertEqual({registered.get(key).metadata.algorithm_id for key in daily_keys}, {
            'robust-z-mad', 'ewma', 'rolling-mean-baseline', 'relative-change',
            'theil-sen-slope', 'kendall-trend',
        })

        query_rank = PersistedInputAdapter(
            adapter_id='mock_query_cohort_projection',
            source_id='mock_query_search', metric='result_rank',
            unit='ordinal result position', source_capabilities=('query_search',),
            runtime_capabilities=('daily_observations', 'input_digest',
                                  'evidence_lineage', 'query_cohort'),
            max_history_days=0, minimum_cohort_size=2,
        )
        query_keys = persisted_keys_for_source(
            registered, 'mock_query_search', input_adapters=(query_rank,),
        )
        self.assertEqual({registered.get(key).metadata.algorithm_id for key in query_keys},
                         {'relative-source-rank'})

        youtube_rank = PersistedInputAdapter(
            adapter_id='youtube_policy_blocked_projection',
            source_id='youtube_video_search', metric='result_rank',
            unit='ordinal result position', source_capabilities=('query_search',),
            runtime_capabilities=('daily_observations', 'input_digest',
                                  'evidence_lineage', 'query_cohort'),
            max_history_days=0, minimum_cohort_size=2,
        )
        self.assertEqual(persisted_keys_for_source(
            registered, 'youtube_video_search', input_adapters=(youtube_rank,),
        ), ())

        google_trends = PersistedInputAdapter(
            adapter_id='google_trends_cluster_projection',
            source_id='google_web_serp', metric='google_trends_cluster_metrics',
            unit='provider-defined per cluster',
            source_capabilities=('trending_snapshot',),
            runtime_capabilities=('daily_observations', 'input_digest',
                                  'evidence_lineage'),
            max_history_days=0,
        )
        self.assertEqual(persisted_keys_for_source(
            registered, 'google_web_serp', input_adapters=(google_trends,),
        ), ())

        wikimedia_keys = persisted_keys_for_source(
            registered, SOURCE,
        )
        self.assertEqual({registered.get(key).metadata.algorithm_id for key in wikimedia_keys}, {
            'growth-ratio', 'median-baseline', 'observed-volume-rank',
            'observed-percentile-rank',
        })
        google_keys = persisted_keys_for_source(registered, 'google_web_serp')
        self.assertEqual({registered.get(key).metadata.algorithm_id for key in google_keys},
                         {'google-trends-increase-rank'})

    def test_relative_source_rank_uses_one_query_cohort_without_self_or_duplicate_items(self):
        scope = Scope('mock_web_search', 'US', 'web', 'query:portable power station',
                      'item-current', 'result_rank', 'ordinal result position')
        peers = (
            Observation(DAY, replace(scope, article='item-current'), 'OBSERVED', None, 'SUCCESS', 1),
            Observation(DAY, replace(scope, article='item-a'), 'OBSERVED', None, 'SUCCESS', 1),
            Observation(DAY, replace(scope, article='item-a'), 'OBSERVED', None, 'SUCCESS', 1),
            Observation(DAY, replace(scope, article='item-b'), 'OBSERVED', None, 'SUCCESS', 3),
            Observation(DAY, replace(scope, article='item-conflict'), 'OBSERVED', None, 'SUCCESS', 2),
            Observation(DAY, replace(scope, article='item-conflict'), 'OBSERVED', None, 'SUCCESS', 4),
            Observation(DAY - timedelta(days=1), replace(scope, article='item-old'),
                        'OBSERVED', None, 'SUCCESS', 100),
        )
        item = AlgorithmInput('candidate', scope, DAY, 3, (), cohort=peers,
                              current_snapshot_status='SUCCESS')
        result = AlgorithmRunner(algorithm_registry()).run(
            item, ['relative-source-rank@v2'],
        )[0]
        self.assertEqual((result.status, result.sample_count, result.value),
                         ('COMPLETED', 3, 25.0))
        self.assertEqual(dict(result.evidence)['query_scope'],
                         ('mock_web_search', 'US', 'web', 'query:portable power station'))
        self.assertNotIn('observed_ranks', dict(result.evidence))

    def test_relative_source_rank_rejects_policy_and_incompatible_query_cohorts(self):
        scope = Scope('mock_web_search', 'US', 'web', 'query:camera',
                      'item-current', 'result_rank', 'ordinal result position')
        peer = Observation(DAY, replace(scope, article='item-peer'), 'OBSERVED', None, 'SUCCESS', 4)
        runner = AlgorithmRunner(algorithm_registry())
        policy = runner.run(replace(
            AlgorithmInput('video-candidate', replace(scope, source='youtube_video_search'),
                           DAY, 2, (), cohort=(peer,)), current_snapshot_status='SUCCESS'),
            ['relative-source-rank@v2'],
        )[0]
        self.assertEqual((policy.status, policy.reason_code), ('SKIPPED', 'POLICY_RESTRICTED'))
        mismatched = replace(peer, scope=replace(peer.scope, project='query:another-camera'))
        incompatible = runner.run(AlgorithmInput(
            'candidate', scope, DAY, 2, (), cohort=(mismatched,),
            current_snapshot_status='SUCCESS',
        ), ['relative-source-rank@v2'])[0]
        self.assertEqual((incompatible.status, incompatible.reason_code),
                         ('SKIPPED', 'INCOMPATIBLE_COHORT'))

    def test_snapshot_popularity_ranks_ties_without_claiming_population_coverage(self):
        cohort = (Observation(DAY, replace(SCOPE, article='Page A'), 'OBSERVED', 500, 'SUCCESS', 2),
                  Observation(DAY, replace(SCOPE, article='Page B'), 'OBSERVED', 500, 'SUCCESS', 3),
                  Observation(DAY, replace(SCOPE, article='Page C'), 'OBSERVED', 100, 'SUCCESS', 4))
        item = replace(make_input(current=500), cohort=cohort, current_snapshot_status='SUCCESS')
        runner = AlgorithmRunner(algorithm_registry())
        results = runner.run(item, ['observed-volume-rank@v4','observed-percentile-rank@v4'])
        values = {row.algorithm_id: row.value for row in results}
        self.assertEqual(values['observed-volume-rank'], 1)
        self.assertAlmostEqual(values['observed-percentile-rank'], 200/3)
        for result in results:
            self.assertEqual((result.status, result.sample_count), ('COMPLETED', 4))
            self.assertTrue({'CENSORED_TOP_LIST', 'PARTIAL_SAMPLE', 'VIEWS_APPROXIMATE'}
                            <= set(result.quality_flags))

    def test_partial_snapshot_duplicate_self_ties_and_repeatable_order(self):
        peers = (
            Observation(DAY, SCOPE, 'OBSERVED', 500, 'PARTIAL', 9),  # self-inclusion
            Observation(DAY, replace(SCOPE, article='Tie'), 'OBSERVED', 500, 'SUCCESS', 10),
            Observation(DAY, replace(SCOPE, article='Peer'), 'OBSERVED', 100, 'PARTIAL', 11),
            Observation(DAY, replace(SCOPE, article='peer'), 'OBSERVED', 100, 'SUCCESS', 12),
            Observation(DAY - timedelta(days=1), replace(SCOPE, article='Wrong date'), 'OBSERVED', 2000, 'SUCCESS', 13),
        )
        item = replace(make_input(current=500), cohort=peers, current_snapshot_status='PARTIAL')
        runner = AlgorithmRunner(algorithm_registry())
        keys = ['observed-volume-rank@v4', 'observed-percentile-rank@v4']
        first = runner.run(item, keys)
        second = runner.run(replace(item, cohort=tuple(reversed(peers))), keys)
        self.assertEqual(first, second)
        values = {row.algorithm_id: (row.status, row.value, row.sample_count) for row in first}
        self.assertEqual(values['observed-volume-rank'], ('COMPLETED', 1, 4))
        self.assertEqual(values['observed-percentile-rank'], ('COMPLETED', 250 / 3, 4))

    def test_snapshot_rank_separates_scope_and_skips_insufficient_or_invalid_values(self):
        mismatched = (
            Observation(DAY, replace(SCOPE, country='CA', article='Canada'), 'OBSERVED', 9000),
            Observation(DAY, replace(SCOPE, access='desktop', article='Desktop'), 'OBSERVED', 8000),
            Observation(DAY, replace(SCOPE, project='fr.wikipedia', article='French'), 'OBSERVED', 7000),
            Observation(DAY, replace(SCOPE, metric='other', article='Metric'), 'OBSERVED', 6000),
            Observation(DAY, replace(SCOPE, unit='other', article='Unit'), 'OBSERVED', 5000),
            Observation(DAY, replace(SCOPE, article='Missing'), 'NOT_IN_TOP', None),
            Observation(DAY, replace(SCOPE, article='Invalid'), 'OBSERVED', -1),
            Observation(DAY - timedelta(days=1), replace(SCOPE, article='Old'), 'OBSERVED', 10000),
        )
        base = replace(make_input(current=0), cohort=mismatched, current_snapshot_status='SUCCESS')
        runner = AlgorithmRunner(algorithm_registry())
        for versioned_key in ('observed-volume-rank@v4', 'observed-percentile-rank@v4'):
            result = runner.run(base, [versioned_key])[0]
            self.assertEqual((result.status, result.reason_code), ('INSUFFICIENT_DATA', 'INSUFFICIENT_COHORT'))
            self.assertEqual((result.sample_count, dict(result.evidence)['cohort_size']), (1, 1))
            self.assertTrue({'CENSORED_TOP_LIST', 'PARTIAL_SAMPLE', 'VIEWS_APPROXIMATE'}
                            <= set(result.quality_flags))
            invalid = runner.run(replace(base, current_value=True), [versioned_key])[0]
            self.assertEqual((invalid.status, invalid.reason_code), ('SKIPPED', 'INVALID_CURRENT'))

    def test_snapshot_rank_skips_unconfirmed_and_zero_is_a_valid_observation(self):
        peer = Observation(DAY, replace(SCOPE, article='Peer'), 'OBSERVED', 0, 'SUCCESS')
        base = replace(make_input(current=0), cohort=(peer,))
        runner = AlgorithmRunner(algorithm_registry())
        for versioned_key in ('observed-volume-rank@v4', 'observed-percentile-rank@v4'):
            partial = runner.run(replace(base, current_snapshot_status='PARTIAL'), [versioned_key])[0]
            self.assertEqual((partial.status, partial.value), ('COMPLETED', 1 if 'volume' in versioned_key else 50.0))
            unknown = runner.run(replace(base, current_snapshot_status='UNKNOWN'), [versioned_key])[0]
            self.assertEqual((unknown.status, unknown.reason_code),
                             ('INSUFFICIENT_DATA', 'CURRENT_COVERAGE_UNCONFIRMED'))


class LegacyStrategyTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_result_mapping_and_insufficient(self):
        _, _, scored = await build_golden()
        runner = algorithm_engine()
        first = scored[0]
        scope = Scope(first.source, first.country_scope, first.access, first.project, first.article_original, METRIC, UNIT)
        item = AlgorithmInput(first.keyword_id,scope,first.observation_date,first.current_views_ceil,(),legacy_result=first)
        mapped = runner.run(item,['wikimedia-trend@v0.1'])[0]
        self.assertEqual((mapped.status,mapped.value),('COMPLETED',first.score))
        self.assertEqual(dict(mapped.evidence)['legacy_status'],first.status)
        insufficient = next(x for x in scored if x.score is None)
        scope2 = replace(scope,project=insufficient.project,article=insufficient.article_original)
        item2 = replace(item,keyword_id=insufficient.keyword_id, scope=scope2,
                        current_value=insufficient.current_views_ceil,legacy_result=insufficient)
        skipped = runner.run(item2,['wikimedia-trend@v0.1'])[0]
        self.assertEqual((skipped.status,skipped.reason_code,skipped.value),('INSUFFICIENT_DATA','INSUFFICIENT_DATA',None))
        self.assertEqual(insufficient.status,'INSUFFICIENT_DATA')
