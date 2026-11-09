"""Offline E2E: synthetic HTTP responses through the actual pipeline and API."""

import asyncio
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

import httpx

from app.adapters.wikimedia import WikimediaAdapter
from app.main import create_app
from app.storage.fake import FakeStorage

DAY = date(2026, 11, 6)
TOKEN = "synthetic-test-operator-token-not-secret"


class SyntheticPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_pipeline_pagination_provenance_and_source_errors(self):
        requests = []
        def handler(request):
            requests.append(request)
            day = date.fromisoformat(request.url.path[-10:].replace('/', '-'))
            offset = (DAY - day).days
            if offset == 6:
                return httpx.Response(404)
            if offset == 7:
                return httpx.Response(429, headers={'Retry-After': '100'})
            records = [{'article': f'Synthetic_topic_{i:02}', 'project': 'en.wikipedia',
                        'rank': i + 1, 'views_ceil': (1000 + i) if offset == 0 else (100 + i)} for i in range(26)]
            records += [{'article': 'Paris_Fashion_Week', 'project': 'en.wikipedia', 'rank': 27, 'views_ceil': 200},
                        {'article': 'Main_Page', 'project': 'en.wikipedia', 'rank': 28, 'views_ceil': 10000}]
            # A second project must not be merged with the same title.
            records += [{'article': 'Synthetic_topic_00', 'project': 'fr.wikipedia', 'rank': 29, 'views_ceil': 300}]
            return httpx.Response(200, json={'items': [{'country':'US','access':'all-access',
                'year': day.strftime('%Y'), 'month':day.strftime('%m'), 'day':day.strftime('%d'), 'articles':records}]})
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = WikimediaAdapter(upstream, max_retry_wait=0)
        class FixedDayAdapter:
            async def collect(self):
                return await adapter.collect(DAY)
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = FakeStorage()
                app = create_app(storage=store, adapter_factory=FixedDayAdapter,
                                 lock_path=str(Path(directory)/'lock'), operator_token=TOKEN, run_mode='synthetic')
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                        self.assertEqual((await client.post('/api/runs')).status_code, 401)
                        response = await client.post('/api/runs', headers={'Authorization':f'Bearer {TOKEN}'})
                        self.assertEqual(response.status_code, 202)
                        run_id=response.json()['run_id']
                        for _ in range(200):
                            run=(await client.get(f'/api/runs/{run_id}')).json()
                            if run['status'] in ('PARTIAL','COMPLETED','FAILED','ABORTED'): break
                            await asyncio.sleep(0.005)
                        self.assertEqual((run['status'],run['mode']),('PARTIAL','synthetic'))
                        self.assertEqual(run['keyword_count'],28)
                        self.assertEqual(run['observation_count'],168)
                        self.assertEqual(len(requests),8)
                        self.assertTrue(all(not request.url.query for request in requests))
                        self.assertEqual({x['http_status'] for x in run['coverage']} & {404,429}, {404,429})
                        first=(await client.get(f'/api/runs/{run_id}/keywords',params={'limit':20})).json()
                        second=(await client.get(f'/api/runs/{run_id}/keywords',params={'limit':20,'offset':first['next_offset']})).json()
                        self.assertEqual((len(first['items']),len(second['items'])),(20,8))
                        self.assertFalse({x['keyword_id'] for x in first['items']} & {x['keyword_id'] for x in second['items']})
                        fashion=(await client.get(f'/api/runs/{run_id}/keywords',params={'category':'fashion'})).json()
                        self.assertEqual(fashion['total'],1)
                        empty=(await client.get(f'/api/runs/{run_id}/keywords',params={'category':'home_goods'})).json()
                        self.assertEqual(empty['empty_reason'],'NO_MATCHES_IN_ANALYZED_DATA')
                        cats=(await client.get(f'/api/runs/{run_id}/keywords',params={'q':'synthetic topic 00'})).json()
                        self.assertEqual(cats['total'],2)
                        self.assertEqual({x['project'] for x in cats['items']},{'en.wikipedia','fr.wikipedia'})
                        detail=(await client.get(f"/api/runs/{run_id}/keywords/{cats['items'][0]['keyword_id']}" )).json()
                        for key in ('source','project','article_original','observation_date','current_views_ceil',
                                    'baseline','score','evidence_url','baseline_observations'):
                            self.assertIn(key,detail)
                        self.assertTrue(detail['evidence_url'].startswith('https://'))
                        self.assertEqual(len(await store.list_records(run_id,'snapshots')),8)
        finally:
            await upstream.aclose()
