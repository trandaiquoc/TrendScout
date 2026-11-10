"""Offline instrumentation, estimation and proposed identity contracts."""
import asyncio
import unittest
from types import SimpleNamespace
import httpx
from app.main import create_app
from app.storage.fake import FakeStorage
from app.storage_analysis.metrics import CountingStorage, json_bytes
from app.storage_analysis.planning import Estimate, check_budget, dataset_key, estimate_scenario


class CountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_operations_failure_and_empty(self):
        fake = FakeStorage(); store = CountingStorage(fake)
        await store.create_run('run', {'status':'COMPLETED'})
        await store.update_run('run', {'status':'COMPLETED'})
        await store.put_record('run','keyword_results','shard-00',{'items':[{'title':'á'}]})
        self.assertEqual(await store.get_run('run'), {'status':'COMPLETED'})
        self.assertIsNone(await store.get_record('run','keyword_results','missing'))
        self.assertEqual(await store.list_records('run','observations'), [])
        self.assertEqual(len(await store.list_records('run','keyword_results')),1)
        with self.assertRaises(Exception): await store.create_run('run',{})
        self.assertEqual((store.metrics['create_run'].calls,store.metrics['create_run'].successes,store.metrics['create_run'].failures),(2,1,1))
        self.assertEqual(store.metrics['put_record'].estimated_writes,1)
        self.assertEqual(store.metrics['get_record'].estimated_reads,1)
        self.assertEqual((store.metrics['list_records'].returned_documents,store.metrics['list_records'].estimated_reads),(1,2))
        self.assertEqual(store.metrics['list_records'].response_json_bytes,json_bytes({'items':[{'title':'á'}]}))
        self.assertGreaterEqual(store.metrics['get_run'].elapsed_seconds,0)

    async def test_api_pagination_repeats_all_shard_reads(self):
        fake=FakeStorage(); run='a'*32
        await fake.create_run(run, {'status':'COMPLETED','keyword_count':16})
        for i in range(16):
            item={'keyword_id':f'{i:x}'+('0'*63), 'search_keyword':f'keyword {i}', 'categories':['unclassified'], 'score':i, 'current_views_ceil':i}
            await fake.put_record(run,'keyword_results',f'shard-{i:02d}',{'items':[item]})
        store=CountingStorage(fake); app=create_app(storage=fake)
        app.state.run_service=SimpleNamespace(storage=store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            for offset in (0,1):
                response=await client.get(f'/api/runs/{run}/keywords',params={'q':'keyword','limit':1,'offset':offset})
                self.assertEqual((response.status_code,len(response.json()['items']),response.json()['total']),(200,1,16))
            detail=await client.get(f"/api/runs/{run}/keywords/"+'0'*64)
            self.assertEqual(detail.status_code,200)
        self.assertEqual(store.metrics['list_records'].estimated_reads,32)
        self.assertEqual(store.metrics['get_run'].estimated_reads,3)
        self.assertEqual(store.metrics['get_record'].estimated_reads,1)

    async def test_concurrent_requests_and_run_isolation(self):
        fake=FakeStorage(); store=CountingStorage(fake)
        for run in ('a','b'):
            await store.create_run(run, {'run':run})
            await store.put_record(run,'keyword_results','shard-00',{'items':[run]})
        results=await asyncio.gather(*(store.list_records('a','keyword_results') for _ in range(10)))
        self.assertEqual(results,[[{'items':['a']}]]*10)
        self.assertEqual(await store.list_records('b','keyword_results'),[{'items':['b']}])
        self.assertEqual(store.metrics['list_records'].estimated_reads,11)


class PlanningTests(unittest.TestCase):
    def test_scenario_and_quota_boundary(self):
        result=estimate_scenario(runs=10,list_requests=1000,detail_requests=10,metadata_requests=10,keyword_bytes=100)
        self.assertEqual((result['writes'],result['reads'],result['new_content_documents'],result['estimated_keyword_payload_bytes']),(2320,17030,2260,100000))
        self.assertTrue(check_budget(Estimate(reads=10),Estimate(reads=10))['allowed'])
        self.assertEqual(check_budget(Estimate(reads=11),Estimate(reads=10))['reason_codes'],['READS_BUDGET_EXCEEDED'])
        with self.assertRaises(ValueError): check_budget(Estimate(),None)
        with self.assertRaises(ValueError): estimate_scenario(runs=-1,list_requests=0)

    def test_shared_dataset_identity(self):
        identity=dict(source='w',country='US',access='all',project='en',analysis_date='2026-01-01',window=7,source_version='v1',cleaning_version='v1',classification_version='v1',algorithm_version='v1',algorithm_config={'x':1})
        key=dataset_key(kind='analysis',identity=identity,share_allowed=True)
        self.assertEqual(key,dataset_key(kind='analysis',identity=dict(reversed(list(identity.items()))),share_allowed=True))
        for field in ('source','country','access','project','window','source_version','cleaning_version','classification_version','algorithm_version','algorithm_config'):
            changed=dict(identity); changed[field]='other'
            self.assertNotEqual(key,dataset_key(kind='analysis',identity=changed,share_allowed=True))
        with self.assertRaises(ValueError): dataset_key(kind='analysis',identity=identity,share_allowed=False)
        with self.assertRaises(ValueError): dataset_key(kind='analysis',identity={},share_allowed=True)


if __name__ == '__main__': unittest.main()
