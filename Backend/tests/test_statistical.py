"""Independent synthetic statistical examples, never live Wikimedia data."""
from dataclasses import replace
from datetime import date, timedelta, datetime
import unittest
from unittest.mock import Mock

from app.algorithms.contracts import AlgorithmInput, Observation, Scope, AlgorithmMetadata
from app.algorithms.registry import AlgorithmRegistry
from app.algorithms.runner import AlgorithmRunner
from app.algorithms.statistical import RobustZMad, RobustConfig, EWMA, EWMAConfig
from app.algorithms.strategies import SOURCE, METRIC, UNIT
from app.composition import algorithm_engine

DAY = date(2026, 1, 31)
SCOPE = Scope(SOURCE,'US','all-access','en.wikipedia','Example',METRIC,UNIT)


def series(values, current, *, scope=SCOPE, window=None):
    length = len(values)
    points = tuple(Observation(DAY - timedelta(days=length-i), scope, 'OBSERVED', value)
                   for i, value in enumerate(values))
    return AlgorithmInput('synthetic-id',scope,DAY,current,points,window_days=window or length,
                          current_snapshot_status='SUCCESS',evidence_url='https://en.wikipedia.org/wiki/Example')


def run(strategy, item):
    registry = AlgorithmRegistry()
    registry.register(strategy)
    return AlgorithmRunner(registry).run(item,[strategy.metadata.key])[0]


class RobustTests(unittest.TestCase):
    def setUp(self): self.strategy = RobustZMad(RobustConfig(allow_top_censored=True))

    def test_median_mad_and_signed_score_without_current_leak(self):
        data = series(list(range(1,16)), 15)
        before = data
        for current in (15, 1, 8, 100000):
            result = run(self.strategy, replace(data, current_value=current))
            self.assertEqual(result.status, 'COMPLETED')
            self.assertEqual(result.baseline_value, 8)
            self.assertEqual(dict(result.evidence)['mad'], 4)
            self.assertAlmostEqual(result.value, .67448975*(current-8)/4)
            self.assertEqual(result.sample_count,15)
            self.assertIn('CONDITIONAL_ON_TOP',result.quality_flags)
            self.assertEqual(result.config_version,'robust-mad-config-v0.1')
        self.assertEqual(data,before)
        self.assertLess(run(self.strategy,replace(data,current_value=1)).value,0)
        self.assertEqual(run(self.strategy,replace(data,current_value=8)).value,0)
        self.assertGreater(run(self.strategy,replace(data,current_value=100000)).value,100)

    def test_mad_zero_and_sample_and_censor_gates(self):
        constant = run(self.strategy,series([10]*14, 20))
        self.assertEqual((constant.status,constant.reason_code,constant.value),('SKIPPED','ZERO_MAD',None))
        self.assertEqual(dict(constant.evidence)['mad'],0)
        self.assertEqual(run(self.strategy,series(list(range(7)), 10)).reason_code,'INSUFFICIENT_SAMPLES')
        full = series(list(range(14)), 10)
        self.assertEqual(run(RobustZMad(),full).reason_code,'CENSORED_SOURCE_NOT_ACCEPTED')
        censored = replace(full.history[0],status='NOT_IN_TOP',value=None)
        self.assertEqual(run(self.strategy,replace(full,history=(censored,)+full.history[1:])).reason_code,'CENSORED_OR_MISSING')
        gap = replace(full,window_days=15)
        self.assertEqual(run(self.strategy,gap).reason_code,'MISSING_DAYS')
        self.assertEqual(run(self.strategy,replace(full,current_snapshot_status='UNKNOWN')).reason_code,'CURRENT_COVERAGE_UNCONFIRMED')
        self.assertEqual(run(self.strategy,replace(full,current_value=None)).reason_code,'INVALID_CURRENT')
        self.assertEqual(run(RobustZMad(RobustConfig(minimum_samples=3,allow_top_censored=True)),full).reason_code,'INVALID_CONFIG')

    def test_scope_time_duplicates_and_invalid(self):
        data = series(list(range(14)), 10)
        for attr,value,reason in [('source','other','UNSUPPORTED_SOURCE'),('metric','likes','UNSUPPORTED_METRIC'),('unit','unknown','UNSUPPORTED_UNIT')]:
            with self.subTest(attr=attr):
                self.assertEqual(run(self.strategy,replace(data,scope=replace(SCOPE,**{attr:value}))).reason_code,reason)
        point = data.history[0]
        cases = [replace(point,scope=replace(SCOPE,project='fr.wikipedia')),
                 replace(point,scope=replace(SCOPE,unit='count'))]
        for bad in cases:
            self.assertEqual(run(self.strategy,replace(data,history=(bad,)+data.history[1:])).reason_code,'INCOMPATIBLE_OBSERVATION')
        self.assertEqual(run(self.strategy,replace(data,history=(replace(point,date=DAY),)+data.history[1:])).reason_code,'INVALID_WINDOW')
        self.assertEqual(run(self.strategy,replace(data,history=(replace(point,date=datetime(2026,1,1)),)+data.history[1:])).reason_code,'INVALID_WINDOW')
        self.assertEqual(run(self.strategy,replace(data,history=(point,point)+data.history[2:])).reason_code,'DUPLICATE_DATE')
        self.assertEqual(run(self.strategy,replace(data,history=(replace(point,value=None),)+data.history[1:])).reason_code,'INVALID_OBSERVATION')
        self.assertEqual(run(self.strategy,replace(data,history=(replace(point,snapshot_status='PARTIAL'),)+data.history[1:])).reason_code,'PARTIAL_COVERAGE')


class EwmaTests(unittest.TestCase):
    def test_hand_calculation_history_and_current(self):
        strategy = EWMA(EWMAConfig(alpha=.5,allow_top_censored=True))
        data = series(list(range(1,8)), 8)
        result = run(strategy,data)
        self.assertEqual((result.status,result.baseline_value,result.value),('COMPLETED',6.015625,7.0078125))
        self.assertEqual(result.sample_count,7)
        self.assertEqual(dict(result.evidence)['historical_ewma'],6.015625)
        self.assertEqual(dict(result.evidence)['current_ewma'],7.0078125)
        self.assertEqual(dict(dict(result.evidence)['config'])['initialization'],'first_historical_observation')
        self.assertEqual(result,run(strategy,replace(data,history=tuple(reversed(data.history)))))
        self.assertEqual(run(strategy,replace(data,current_value=100)).baseline_value,6.015625)
        self.assertEqual(run(EWMA(EWMAConfig(alpha=1,allow_top_censored=True)),data).value,8)

    def test_constant_decrease_warmup_and_missing(self):
        strategy = EWMA(EWMAConfig(alpha=.5,allow_top_censored=True))
        self.assertEqual(run(strategy,series([9]*7,9)).value,9)
        declining = run(strategy,series([7,6,5,4,3,2,1],0))
        self.assertLess(declining.value,declining.baseline_value)
        self.assertEqual(run(strategy,series([1]*6,1)).reason_code,'INSUFFICIENT_SAMPLES')
        self.assertEqual(run(strategy,series([1]*7,1,window=8)).reason_code,'MISSING_DAYS')
        data = series([1]*7,1)
        bad = replace(data.history[0],status='NOT_IN_TOP',value=None)
        self.assertEqual(run(strategy,replace(data,history=(bad,)+data.history[1:])).reason_code,'CENSORED_OR_MISSING')
        self.assertEqual(run(EWMA(),data).reason_code,'CENSORED_SOURCE_NOT_ACCEPTED')
        self.assertEqual(run(strategy,replace(data,current_snapshot_status='UNKNOWN')).reason_code,'CURRENT_COVERAGE_UNCONFIRMED')

    def test_config_and_mixed_input(self):
        data = series(list(range(1,8)), 8)
        for alpha in (0,1.1,float('nan'),True):
            self.assertEqual(run(EWMA(EWMAConfig(alpha=alpha,allow_top_censored=True)),data).reason_code,'INVALID_CONFIG')
        self.assertEqual(run(EWMA(EWMAConfig(warmup=3,allow_top_censored=True)),data).reason_code,'INVALID_CONFIG')
        strategy = EWMA(EWMAConfig(allow_top_censored=True))
        invalid = replace(data.history[0], scope=replace(SCOPE,metric='likes'))
        self.assertEqual(run(strategy,replace(data,history=(invalid,)+data.history[1:])).reason_code,'INCOMPATIBLE_OBSERVATION')
        self.assertEqual(run(strategy,replace(data,history=(data.history[0],data.history[0])+data.history[2:])).reason_code,'DUPLICATE_DATE')


class RegistryTests(unittest.TestCase):
    def test_selected_together_complete_and_skip(self):
        registry = AlgorithmRegistry()
        registry.register(RobustZMad())
        registry.register(EWMA(EWMAConfig(alpha=.5, allow_top_censored=True)))
        data = series(list(range(1,15)), 15)
        results = AlgorithmRunner(registry).run(data,['robust-z-mad@v0.1','ewma@v0.1'])
        self.assertEqual([x.algorithm_id for x in results], ['ewma','robust-z-mad'])
        self.assertEqual([x.status for x in results], ['COMPLETED','SKIPPED'])
        self.assertEqual(dict(results[0].evidence)['total_sample_count'], 15)
        self.assertIsNone(results[1].value)

    def test_registered_disabled_isolation_and_unknown(self):
        engine = algorithm_engine()
        self.assertIn('robust-z-mad@v0.1',engine.registry.keys())
        self.assertIn('ewma@v0.1',engine.registry.keys())
        self.assertEqual(engine.run(series([1]*7,1),[]),())
        with self.assertRaises(ValueError): engine.run(series([1]*7,1),['unknown@v1'])
        registry = AlgorithmRegistry()
        registry.register(EWMA())
        with self.assertRaises(ValueError): registry.register(EWMA())
        broken = Mock()
        broken.metadata = AlgorithmMetadata('broken','v1','Broken',SOURCE,METRIC,UNIT,3,90)
        broken.applicable.side_effect = RuntimeError('sensitive synthetic')
        registry.register(broken)
        result = AlgorithmRunner(registry).run(series([1]*7,1),['ewma@v0.1','broken@v1'])
        self.assertEqual([r.status for r in result],['FAILED','SKIPPED'])
        self.assertNotIn('sensitive',repr(result))
