"""Independent, synthetic-only evaluation and temporal regression tests."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import unittest

from app.algorithms.evaluation.offline import load_dataset, validate_dataset, evaluate, prediction_from_result
from app.algorithms.statistical import RobustZMad, RobustConfig, EWMA, EWMAConfig
from test_statistical import DAY, series, run

FIXTURE = Path(__file__).parent / 'fixtures/evaluation_v1/synthetic.json'


def predictions(dataset, algorithm='volume-only@v0.1'):
    metric, unit = (('views_ceil', 'approx_pageviews_upper_bound') if algorithm.startswith('volume') else ('trend_score', 'points_0_100'))
    return [dict(entity_id=row['entity_id'], algorithm=algorithm, status='SKIPPED' if row['entity_id']=='F' else 'COMPLETED',
                 value=None if row['entity_id']=='F' else row['current_value'], reason='INSUFFICIENT_SAMPLES' if row['entity_id']=='F' else None,
                 metric=metric, unit=unit, window_days=dataset['window_days'], scope=dataset['scope'],
                 evaluation_date=dataset['evaluation_date'], feature_dates=['2026-01-30','2026-01-31']) for row in dataset['entities']]


class TemporalTests(unittest.TestCase):
    def test_robust_separate_15_and_14_historical_values(self):
        strategy = RobustZMad(RobustConfig(allow_top_censored=True))
        first = series(list(range(1,16)),15)
        self.assertEqual((first.history[0].date.isoformat(), first.history[-1].date.isoformat(), first.analysis_date.isoformat()),('2026-01-16','2026-01-30','2026-01-31'))
        result = run(strategy,first)
        self.assertEqual((result.baseline_value,dict(result.evidence)['mad']),(8,4))
        self.assertAlmostEqual(result.value,1.1803570625)
        second = run(strategy,series(list(range(1,15)),15))
        self.assertEqual((second.baseline_value,dict(second.evidence)['mad']),(7.5,3.5))
        self.assertAlmostEqual(second.value,1.4453351785714286)

    def test_current_and_future_rejected_for_both_strategies(self):
        data=series(list(range(1,16)),15)
        for strategy in (RobustZMad(RobustConfig(allow_top_censored=True)), EWMA(EWMAConfig(allow_top_censored=True))):
            for day in (DAY, DAY+timedelta(days=1)):
                bad=replace(data.history[-1],date=day)
                self.assertEqual(run(strategy,replace(data,history=data.history[:-1]+(bad,))).reason_code,'INVALID_WINDOW')
            self.assertEqual(run(strategy,replace(data,history=data.history[:-1]+(data.history[0],))).reason_code,'DUPLICATE_DATE')
        ewma=EWMA(EWMAConfig(alpha=.5,allow_top_censored=True))
        self.assertEqual(run(ewma,data).baseline_value,run(ewma,replace(data,current_value=999)).baseline_value)


class DatasetTests(unittest.TestCase):
    def setUp(self): self.data=load_dataset(FIXTURE)

    def test_valid_and_invalid_contract(self):
        self.assertEqual(len(self.data['entities']),6)
        changes=[lambda d:d.update(dataset_version=''), lambda d:d['entities'].append(deepcopy(d['entities'][0])),
                 lambda d:d['entities'][0].pop('label'), lambda d:d['entities'][0]['label'].update(value='MAYBE'),
                 lambda d:d['entities'][0]['label'].update(reviewer_id=''), lambda d:d['entities'][0]['label'].update(review_timestamp='bad'),
                 lambda d:d['entities'][0].update(scope={}), lambda d:d['entities'][0]['history'][0].update(date='2026-01-31'),
                 lambda d:d['entities'][0]['history'][0].update(date='2026-02-01'), lambda d:d['entities'][0]['label'].update(evidence_as_of='2026-02-01'),
                 lambda d:d['entities'][0]['history'].append(deepcopy(d['entities'][0]['history'][0]))]
        for change in changes:
            d=deepcopy(self.data); change(d)
            with self.subTest(change=change), self.assertRaises(ValueError): validate_dataset(d)

    def test_labels_not_features(self):
        p=predictions(self.data)
        baseline=evaluate(self.data,{'volume-only@v0.1':p},1,'test')
        changed=deepcopy(self.data); changed['entities'][0]['label']['value']='NEGATIVE'
        another=evaluate(changed,{'volume-only@v0.1':p},1,'test')
        self.assertEqual(baseline['common_universe'],another['common_universe'])
        self.assertEqual(baseline['algorithms']['volume-only@v0.1']['ranked_entity_ids'],another['algorithms']['volume-only@v0.1']['ranked_entity_ids'])
        self.assertNotEqual(baseline['algorithms']['volume-only@v0.1']['precision_at_k'],another['algorithms']['volume-only@v0.1']['precision_at_k'])


class EvaluationTests(unittest.TestCase):
    def setUp(self): self.data=load_dataset(FIXTURE); self.key='volume-only@v0.1'; self.p=predictions(self.data)
    def report(self,k=1,p=None): return evaluate(self.data,{self.key:self.p if p is None else p},k,'synthetic-test')['algorithms'][self.key]

    def test_precision_false_positive_and_coverage(self):
        self.assertEqual(self.report(1)['precision_at_k'],1)
        r=self.report(4)
        self.assertEqual(r['precision_at_k'],.5)
        self.assertEqual(r['false_positive_count_reviewed_top_k'],2)
        self.assertEqual(r['false_positive_share_reviewed_top_k'],.5)
        self.assertEqual((r['completed'],r['skipped'],r['failed'],r['labeled_count']),(5,1,0,4))
        self.assertEqual(r['coverage'],5/6)
        self.assertEqual(r['skip_rate'],1/6)
        self.assertEqual(self.report(5)['status'],'INSUFFICIENT_LABELS')
        self.assertIsNone(self.report(5)['precision_at_k'])
        self.assertEqual(self.report(10)['status'],'INSUFFICIENT_RESULTS')
        for k in (0,-1,True):
            with self.assertRaises(ValueError): self.report(k)

    def test_precision_at_five_and_ten_and_no_positives(self):
        d=deepcopy(self.data)
        for i in range(6,10):
            row=deepcopy(d['entities'][0]); row['entity_id']=chr(65+i); row['current_value']=100-i*10
            row['label']['value']='NEGATIVE'; d['entities'].append(row)
        for row in d['entities']:
            if row['label']['value'] in ('UNCERTAIN','NOT_REVIEWED'):
                row['label'].update(value='NEGATIVE',reviewer_id='synthetic',review_timestamp='2026-02-01T00:00:00Z',disagreement=False)
        p=predictions(d)
        p[5].update(status='COMPLETED',value=50,reason=None)
        self.assertEqual(evaluate(d,{self.key:p},5,'five')['algorithms'][self.key]['precision_at_k'],2/5)
        self.assertEqual(evaluate(d,{self.key:p},10,'ten')['algorithms'][self.key]['precision_at_k'],2/10)
        for row in d['entities']: row['label']['value']='NEGATIVE'
        self.assertEqual(evaluate(d,{self.key:p},10,'zero')['algorithms'][self.key]['precision_at_k'],0)

    def test_same_universe_and_uncomparable(self):
        key='wikimedia-trend@v0.1'; other=predictions(self.data,key)
        other[0].update(status='FAILED',value=None,reason='ERROR')
        report=evaluate(self.data,{self.key:self.p,key:other,'median-baseline@v0.1':[],'ewma@v0.1':[],'robust-z-mad@v0.1':[]},3,'test')
        self.assertEqual(report['common_universe'],['B','C','D','E'])
        self.assertEqual(report['algorithms'][key]['failed'],1)
        self.assertEqual(report['algorithms'][self.key]['precision_at_k'],1/3)
        self.assertEqual(report['algorithms']['ewma@v0.1']['status'],'NOT_COMPARABLE')
        self.assertEqual(report,evaluate(self.data,{self.key:self.p,key:other,'median-baseline@v0.1':[],'ewma@v0.1':[],'robust-z-mad@v0.1':[]},3,'test'))

    def test_bad_results_and_context(self):
        for mutation in (lambda p:p[0].update(unit='likes'),lambda p:p[0].update(value=None),
                         lambda p:p[0].update(feature_dates=['2026-02-01']),lambda p:p.append(deepcopy(p[0])),
                         lambda p:p[0].update(scope={}),lambda p:p[5].update(value=0)):
            p=deepcopy(self.p); mutation(p)
            with self.assertRaises(ValueError): self.report(1,p)
        p=deepcopy(self.p)
        for row in p: row.update(status='SKIPPED',value=None,reason='MISSING')
        self.assertEqual(self.report(1,p)['status'],'INSUFFICIENT_RESULTS')

    def test_ties(self):
        key='wikimedia-trend@v0.1'; p=predictions(self.data,key)
        p[0]['value']=p[1]['value']=50
        for x in p[2:5]: x['value']=10
        r=evaluate(self.data,{key:list(reversed(p))},2,'test')['algorithms'][key]
        self.assertEqual(r['top_k_entity_ids'],['A','B'])

    def test_adapter_preserves_result(self):
        result=run(RobustZMad(RobustConfig(allow_top_censored=True)),series(list(range(1,16)),15))
        adapted=prediction_from_result(result,scope=self.data['scope'],evaluation_date='2026-01-31',feature_dates=['2026-01-30'])
        self.assertEqual((adapted['algorithm'],adapted['value']),('robust-z-mad@v0.1',result.value))


if __name__=='__main__': unittest.main()
