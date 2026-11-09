"""Synthetic algorithm contracts; no provider access."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import unittest
import subprocess
import sys
from unittest.mock import Mock

from app.algorithms.contracts import AlgorithmInput, AlgorithmMetadata, Observation, Scope
from app.algorithms.registry import AlgorithmRegistry
from app.algorithms.runner import AlgorithmRunner
from app.algorithms.strategies import GrowthRatio, MedianBaseline, SOURCE, METRIC, UNIT, WikimediaLegacy
from app.composition import algorithm_engine
from test_golden_contract import build_golden

DAY = date(2026,1,8)
SCOPE = Scope(SOURCE,'US','all-access','en.wikipedia','Example',METRIC,UNIT)


def make_input(values=(100,200,300), current=800):
    history = tuple(Observation(DAY-timedelta(days=i+1),SCOPE,'OBSERVED',v) for i,v in enumerate(values))
    return AlgorithmInput('synthetic-id', SCOPE, DAY, current, history, evidence_url='https://en.wikipedia.org/wiki/Example')


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
        with self.assertRaises(ValueError): registry.register(strategy)
        with self.assertRaises(ValueError): AlgorithmRunner(registry).run(make_input(), ['unknown@v1'])
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
        self.assertEqual(output[0].status,'SKIPPED')
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
        self.assertEqual(len(dict(odd[0].evidence)['observations']), 3)
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
                 (replace(base, current_value=None), 'INVALID_CURRENT'),
                 (replace(base, history=tuple(replace(x,value=0) for x in base.history)), 'ZERO_BASELINE'),
                 (replace(base, history=(replace(base.history[0],scope=replace(SCOPE,project='fr.wikipedia')),)+base.history[1:]), 'INCOMPATIBLE_OBSERVATION'),
                 (replace(base, history=(replace(base.history[0],value=None),)+base.history[1:]), 'INVALID_OBSERVATION')]
        for item, code in cases:
            with self.subTest(code=code):
                result = self.runner.run(item, ['growth-ratio@v0.1'])[0]
                self.assertEqual((result.status,result.reason_code,result.value), ('SKIPPED',code,None))
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
        with self.assertRaises(ValueError): runner.legacy_results(DAY, [])


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
        self.assertEqual((skipped.status,skipped.reason_code,skipped.value),('SKIPPED','INSUFFICIENT_DATA',None))
        self.assertEqual(insufficient.status,'INSUFFICIENT_DATA')
