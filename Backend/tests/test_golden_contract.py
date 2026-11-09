"""Versioned, synthetic golden regression. No network, ADC, or cloud writes."""
import copy
import hashlib
import json
import math
import tempfile
import unittest
from datetime import date, datetime
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from app.adapters.wikimedia import WikimediaAdapter
from app.analysis.keywords import extract_keywords
from app.analysis.trend import DayData, score_trends
from app.main import create_app
from app.processing.wikimedia_cleaning import clean_snapshot
from app.storage.fake import FakeStorage

FIXTURES = Path(__file__).parent / 'fixtures' / 'golden_v1'
SOURCE = json.loads((FIXTURES / 'source.json').read_text(encoding='utf-8'))
EXPECTED = json.loads((FIXTURES / 'expected.json').read_text(encoding='utf-8'))
RUN = 'a' * 32


async def build_golden():
    def handler(request):
        key = request.url.path[-10:].replace('/', '-')
        entry = SOURCE['days'][key]
        if entry['http'] != 200:
            return httpx.Response(entry['http'], headers={'Retry-After': '0'})
        year, month, day = key.split('-')
        return httpx.Response(200, json={'items': [{**SOURCE['scope'], 'year': year,
            'month': month, 'day': day, 'articles': entry['articles']}]})
    data, cleaned = [], []
    fixed = datetime.fromisoformat(SOURCE['fetched_at'])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = WikimediaAdapter(client, max_retries=0)
        with patch('app.adapters.wikimedia.datetime', wraps=datetime) as clock:
            clock.now.return_value = fixed
            for day in sorted(SOURCE['days'], reverse=True):
                snapshot, rows = await adapter.fetch_day(date.fromisoformat(day))
                result = clean_snapshot(snapshot, rows)
                data.append(DayData(snapshot, tuple(extract_keywords(result.accepted)), tuple(rows)))
                cleaned.append(result)
    scored = score_trends(date.fromisoformat(SOURCE['analysis_date']), data)
    return data, cleaned, scored


def assert_expected(actual, expected):
    for key, value in expected.items():
        if key not in actual:
            raise AssertionError(f'{key}: required field missing')
        if actual[key] != value:
            raise AssertionError(f'{key}: expected {value!r}, got {actual[key]!r}')


class GoldenPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_pipeline_and_independent_math(self):
        data, clean, scores = await build_golden()
        self.assertEqual(SOURCE['mode'], 'SYNTHETIC')
        self.assertEqual(SOURCE['fixture_version'], EXPECTED['fixture_version'])
        report = clean[0].report
        self.assertEqual({k: getattr(report, k) for k in EXPECTED['cleaning']}, EXPECTED['cleaning'])
        self.assertEqual(report.raw_count, report.accepted_count + report.rejected_count + report.duplicate_count)
        rows = {r.keyword_id: r.to_record() for r in scores}
        self.assertEqual(len(rows), len(EXPECTED['entities']))
        for expected in EXPECTED['entities']:
            row = rows[expected['keyword_id']]
            assert_expected(row, expected)
            for field in ('source', 'country_scope', 'access', 'observation_date', 'algorithm_version'):
                self.assertEqual(row[field], EXPECTED[field])
            self.assertIn('CENSORED_TOP_LIST', row['quality_flags'])
            self.assertIn('VIEWS_APPROXIMATE', row['quality_flags'])
            if row['score'] is not None:
                ratio = row['current_views_ceil'] / row['baseline']
                growth = min(1, max(0, math.log2(ratio) / 3))
                volume = math.log1p(row['current_views_ceil']) / math.log1p(800)
                self.assertEqual(row['score'], round(100 * (0.7 * growth + 0.3 * volume)))
                self.assertEqual(row['score_components']['max_views_ceil'], 800)
                self.assertAlmostEqual(row['score_components']['growth'], growth)
                self.assertAlmostEqual(row['score_components']['volume'], volume)
                self.assertEqual(row['observed_baseline_days'], 3)
                self.assertEqual({x['reason'] for x in row['missing_baseline_dates']},
                                 {'DAY_UNAVAILABLE', 'FETCH_ERROR', 'NOT_IN_TOP', 'SNAPSHOT_MISSING'})
        current = data[0].keywords[0]
        previous = data[1].keywords[0]
        self.assertEqual(current.keyword_id, previous.keyword_id)
        self.assertNotEqual(current.observation_id, previous.observation_id)
        identity = ['wikimedia_pageviews_top_per_country', 'US', 'all-access', 'en.wikipedia', 'Fashion_Week', '2026-01-08']
        expected_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(current.observation_id, expected_id)
        self.assertEqual([(e.category, e.rule_id, e.matched_text) for e in current.classification_evidence],
                         [('fashion', 'fashion_industry', 'Fashion Week')])
        self.assertEqual(current.classification_version, EXPECTED['classification_version'])
        self.assertEqual(current.fetched_at.isoformat(), SOURCE['fetched_at'])
        self.assertEqual(current.raw_record['views_ceil'], 800)
        self.assertEqual(current.rank, 1)
        altered = copy.deepcopy(EXPECTED['entities'][0])
        altered['score'] = 99
        with self.assertRaises(AssertionError):
            assert_expected(rows[altered['keyword_id']], altered)
        missing = dict(rows[EXPECTED['entities'][0]['keyword_id']])
        del missing['score']
        with self.assertRaises(AssertionError):
            assert_expected(missing, EXPECTED['entities'][0])

    async def test_scoring_thresholds_and_zero_baseline(self):
        data, _, _ = await build_golden()
        # Limit to one entity per scope so current is also the daily maximum.
        def run_case(current, previous):
            days = []
            for i, item in enumerate(data):
                if i > 3:
                    break
                row = item.keywords[0]
                row = replace(row, views_ceil=current if i == 0 else previous)
                days.append(DayData(item.snapshot, (row,), item.source_rows))
            return score_trends(date.fromisoformat(SOURCE['analysis_date']), days)[0]
        for current, expected_score, status in [(150, 44, 'STABLE'), (181, 50, 'EMERGING'),
                                                 (75, 30, 'STABLE'), (74, 30, 'FALLING')]:
            with self.subTest(current=current):
                result = run_case(current, 100)
                self.assertEqual((result.score, result.status), (expected_score, status))
        self.assertIsNone(run_case(0, 100).score)
        self.assertEqual(run_case(100, 0).status, 'INSUFFICIENT_DATA')
        self.assertIsNone(run_case(100, 0).score)

    async def test_repeatability(self):
        first = [x.to_record() for x in (await build_golden())[2]]
        second = [x.to_record() for x in (await build_golden())[2]]
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(first, sort_keys=True, ensure_ascii=False),
                         json.dumps(second, sort_keys=True, ensure_ascii=False))


class StoredApiGoldenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = FakeStorage()
        data, _, scores = await build_golden()
        current = {row.keyword_id: row.to_record() for row in data[0].keywords}
        for score in scores:
            current[score.keyword_id].update(score.to_record())
        # Add a zero score beside null to lock API sorting; this is synthetic stored data.
        rows = list(current.values())
        rows[3]['score'] = 0
        await self.store.create_run(RUN, {'run_id': RUN, 'status': 'COMPLETED', 'mode': 'synthetic', 'keyword_count': len(rows)})
        shards = [[] for _ in range(16)]
        for row in rows:
            shards[int(row['keyword_id'][0], 16)].append(row)
        for index, items in enumerate(shards):
            await self.store.put_record(RUN, 'keyword_results', f'shard-{index:02d}', {'items': items})
        await self.store.put_record(RUN, 'snapshots', '2026-01-08', {'fetch_status':'PARTIAL'})
        await self.store.put_record(RUN, 'observations', '2026-01-08-0000', {'items': rows})
        self.app = create_app(storage=self.store, lock_path=str(Path(self.temp.name)/'lock'), operator_token='synthetic-operator-token-123456', run_mode='synthetic')
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.life.__aexit__(None, None, None)
        self.temp.cleanup()

    async def test_api_contract_and_pagination(self):
        path = f'/api/runs/{RUN}/keywords'
        self.assertEqual((await self.client.get('/api/health')).json(), {'status':'ok'})
        self.assertEqual((await self.client.post('/api/runs')).status_code, 401)
        self.assertEqual((await self.client.get('/api/runs/not-valid')).status_code, 404)
        self.assertEqual((await self.client.get(f'/api/runs/{"b"*32}')).status_code, 404)
        self.assertEqual((await self.client.get(path, params={'limit':0})).status_code, 422)
        self.assertEqual((await self.client.get(path, params={'category':'other'})).status_code, 422)
        with patch('app.adapters.wikimedia.WikimediaAdapter.collect', side_effect=AssertionError('unexpected fetch')):
            response = await self.client.get(path, params={'q':'fashion week', 'sort':'score', 'limit':1})
        self.assertEqual(response.status_code, 200)
        page = response.json()
        self.assertEqual(set(page), {'items','total','offset','limit','next_offset','empty_reason'})
        self.assertEqual((page['total'], page['offset'], page['limit'], page['next_offset']), (2,0,1,1))
        second = (await self.client.get(path, params={'q':'fashion week','sort':'score','limit':1,'offset':1})).json()
        self.assertNotEqual(page['items'][0]['keyword_id'], second['items'][0]['keyword_id'])
        for category, total in [('all',6),('fashion',2),('home_goods',1),('unclassified',2)]:
            self.assertEqual((await self.client.get(path, params={'category':category})).json()['total'], total)
        empty = (await self.client.get(path, params={'q':'no-match'})).json()
        self.assertEqual((empty['items'], empty['total'], empty['empty_reason']), ([],0,'NO_MATCHES_IN_ANALYZED_DATA'))
        results = (await self.client.get(path)).json()['items']
        self.assertEqual(results[-2]['score'], 0)
        self.assertIsNone(results[-1]['score'])
        tied = [x['keyword_id'] for x in results if x['score'] == 21]
        self.assertEqual(tied, sorted(tied))
        keyword_id = EXPECTED['entities'][0]['keyword_id']
        detail = await self.client.get(f'{path}/{keyword_id}')
        self.assertEqual(detail.status_code, 200)
        assert_expected(detail.json(), EXPECTED['entities'][0])
        self.assertEqual((await self.client.get(f'{path}/{"f"*64}')).status_code, 404)
        self.assertEqual((await self.client.get(f'{path}/bad-id')).status_code, 404)
        self.assertEqual((await self.store.get_record(RUN,'snapshots','2026-01-08'))['fetch_status'],'PARTIAL')
        self.assertEqual(len((await self.store.get_record(RUN,'observations','2026-01-08-0000'))['items']),6)

    async def test_parent_removal_does_not_imply_subcollection_removal_in_fake(self):
        # Fake has no public delete API; simulate parent disappearance without a
        # recursive cleanup. This is not a Firestore emulator or a cloud test.
        self.store._runs.pop(RUN)
        self.assertIsNone(await self.store.get_run(RUN))
        self.assertIsNotNone(await self.store.get_record(RUN, 'snapshots', '2026-01-08'))
        self.assertEqual((await self.client.get(f'/api/runs/{RUN}/keywords')).status_code, 404)

    async def test_read_after_new_application_instance(self):
        second = create_app(storage=self.store, lock_path=str(Path(self.temp.name)/'lock2'))
        async with second.router.lifespan_context(second):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url='http://test') as client:
                self.assertEqual((await client.get(f'/api/runs/{RUN}')).json()['status'], 'COMPLETED')
                self.assertEqual((await client.get(f'/api/runs/{RUN}/keywords')).json()['total'], 6)

class PersistenceOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_only_after_all_snapshot_and_result_shards_written(self):
        import asyncio
        from app.adapters.wikimedia import CollectionResult
        from app.models.observations import FetchStatus
        from test_runs import fixture

        sample = fixture()
        days = []
        for snapshot, rows in sample.days:
            if snapshot.fetch_status != FetchStatus.SUCCESS:
                snapshot = replace(snapshot, fetch_status=FetchStatus.SUCCESS, http_status=200,
                                   item_count=0, rejected_count=0, error=None)
            days.append((snapshot, rows))

        class SyntheticAdapter:
            async def collect(self):
                return CollectionResult(sample.analysis_date, tuple(days))

        class CheckingStorage(FakeStorage):
            async def update_run(self, run_id, fields):
                if fields.get('status') == 'COMPLETED':
                    assert len(await self.list_records(run_id, 'snapshots')) == 8
                    assert len(await self.list_records(run_id, 'keyword_results')) == 16
                    assert len(await self.list_records(run_id, 'observations')) > 0
                await super().update_run(run_id, fields)

        store = CheckingStorage()
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(storage=store, lock_path=str(Path(temp)/'lock'),
                             operator_token='synthetic-operator-token-123456',
                             adapter_factory=SyntheticAdapter, run_mode='synthetic')
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    response = await client.post('/api/runs', headers={'Authorization':'Bearer synthetic-operator-token-123456'})
                    self.assertEqual(response.status_code, 202)
                    payload = response.json()
                    self.assertEqual(set(payload), {'run_id', 'status'})
                    self.assertEqual(len(payload['run_id']), 32)
                    for _ in range(200):
                        run = (await client.get('/api/runs/' + payload['run_id'])).json()
                        if run['status'] in ('COMPLETED', 'PARTIAL', 'FAILED', 'ABORTED'):
                            break
                        await asyncio.sleep(0.005)
                    self.assertEqual(run['status'], 'COMPLETED')
