"""Shared offline repository contract: fake and Firestore with a stubbed SDK.

This stub checks the SDK calls and old document layout. It does not emulate all
Firestore semantics and does not contact Firestore, ADC or an emulator.
"""
import unittest
from copy import deepcopy
from unittest.mock import patch

from google.api_core.exceptions import AlreadyExists, NotFound, PermissionDenied

from app.ports.storage import AnalysisStorage, RecordAlreadyExists, RecordNotFound, StorageError
from app.storage.fake import FakeStorage
from app.storage.firestore import FirestoreStorage

RUN = '30e96cf96abe48dbb304fb9808c89873'


class Snapshot:
    def __init__(self, value):
        self.exists = value is not None
        self.value = deepcopy(value)

    def to_dict(self):
        return deepcopy(self.value)


class Document:
    def __init__(self, data, path):
        self.data, self.path = data, path

    def collection(self, name):
        return Collection(self.data, self.path + (name,))

    def create(self, value):
        if self.path in self.data:
            raise AlreadyExists('already exists')
        self.data[self.path] = deepcopy(value)

    def update(self, value):
        if self.path not in self.data:
            raise NotFound('not found')
        self.data[self.path].update(deepcopy(value))

    def set(self, value):
        self.data[self.path] = deepcopy(value)

    def get(self):
        return Snapshot(self.data.get(self.path))


class Collection:
    def __init__(self, data, path, order=None, limit_value=None, offset_value=0):
        self.data, self.path = data, path
        self.order, self.limit_value, self.offset_value = order, limit_value, offset_value

    def document(self, name):
        return Document(self.data, self.path + (name,))

    def order_by(self, field, direction=None):
        return Collection(self.data, self.path, (field, direction), self.limit_value, self.offset_value)

    def offset(self, value):
        return Collection(self.data, self.path, self.order, self.limit_value, value)

    def limit(self, value):
        return Collection(self.data, self.path, self.order, value, self.offset_value)

    def stream(self):
        values = [value for path, value in self.data.items() if path[:-1] == self.path]
        if self.order:
            field, direction = self.order
            values.sort(key=lambda value: str(value.get(field) or ""), reverse=direction == "DESCENDING")
        values = values[self.offset_value : self.offset_value + self.limit_value if self.limit_value is not None else None]
        return [Snapshot(value) for value in values]


class StubFirestoreClient:
    def __init__(self):
        self.data = {}

    def collection(self, name):
        return Collection(self.data, (name,))


class ContractMixin:
    async def test_protocol_run_roundtrip_and_null_missing_zero(self):
        self.assertIsInstance(self.store, AnalysisStorage)
        await self.store.create_run(RUN, {'status':'QUEUED', 'score':None, 'views':0})
        await self.store.update_run(RUN, {'status':'FETCHING'})
        record = await self.store.get_run(RUN)
        self.assertEqual(record, {'status':'FETCHING', 'score':None, 'views':0})
        self.assertNotIn('baseline', record)
        record['views'] = 500
        self.assertEqual((await self.store.get_run(RUN))['views'], 0)
        self.assertIsNone(await self.store.get_run('missing'))
        with self.assertRaises(RecordAlreadyExists):
            await self.store.create_run(RUN, {})
        with self.assertRaises(RecordNotFound):
            await self.store.update_run('missing', {'status':'FAILED'})

    async def test_snapshot_buckets_and_legacy_shard_unchanged(self):
        await self.store.create_run(RUN, {'status':'COMPLETED', 'keyword_count':1})
        entries = [('snapshots','2026-01-08',{'fetch_status':'SUCCESS','http_status':200}),
                   ('observations','2026-01-08-0000',{'items':[{'views_ceil':0, 'baseline':None}]}),
                   ('keyword_results','shard-00',{'items':[{'keyword_id':'0'*64, 'score':None,
                     'baseline':None, 'quality_flags':['CENSORED_TOP_LIST'], 'evidence_url':'https://en.wikipedia.org/wiki/Example'}]})]
        for kind, key, data in entries:
            await self.store.put_record(RUN, kind, key, data)
            self.assertEqual(await self.store.get_record(RUN, kind, key), data)
            self.assertEqual(await self.store.list_records(RUN, kind), [data])
            self.assertIsNone(await self.store.get_record(RUN, kind, 'missing'))
        self.assertNotIn('ratio', (await self.store.get_record(RUN,'keyword_results','shard-00'))['items'][0])

    async def test_run_history_is_ordered_and_paginated(self):
        await self.store.create_run('a' * 32, {'status': 'COMPLETED', 'created_at': '2026-01-01T00:00:00+00:00'})
        await self.store.create_run('b' * 32, {'status': 'PARTIAL', 'created_at': '2026-01-03T00:00:00+00:00'})
        await self.store.create_run('c' * 32, {'status': 'FAILED', 'created_at': '2026-01-02T00:00:00+00:00'})
        page = await self.store.list_runs(limit=2, offset=0)
        self.assertEqual([item['status'] for item in page], ['PARTIAL', 'FAILED'])
        self.assertEqual([item['status'] for item in await self.store.list_runs(limit=2, offset=2)], ['COMPLETED'])

    async def test_overwrite_error_and_paths(self):
        await self.store.put_record(RUN, 'observations', 'a', {'old':1})
        await self.store.put_record(RUN, 'observations', 'a', {'new':0})
        self.assertEqual(await self.store.get_record(RUN,'observations','a'), {'new':0})
        with self.assertRaises(ValueError):
            await self.store.get_record(RUN,'unknown','a')
        with self.assertRaises(ValueError):
            await self.store.get_run('invalid/id')


class FakeRepositoryContract(ContractMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeStorage()


class FirestoreRepositoryContract(ContractMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = StubFirestoreClient()
        self.store = FirestoreStorage(self.client)

    async def test_layout_and_provider_failure_are_preserved(self):
        await self.store.create_run(RUN, {'status':'QUEUED'})
        await self.store.put_record(RUN,'keyword_results','shard-00',{'items':[]})
        self.assertIn(('analysis_runs',RUN), self.client.data)
        self.assertIn(('analysis_runs',RUN,'keyword_results','shard-00'), self.client.data)
        with patch.object(Document, 'set', side_effect=PermissionDenied('denied')):
            with self.assertRaises(StorageError) as error:
                await self.store.put_record(RUN,'observations','b',{'items':[]})
        self.assertIsInstance(error.exception.__cause__, PermissionDenied)

    async def test_recreate_repository_over_same_stub(self):
        await self.store.create_run(RUN, {'status':'COMPLETED', 'keyword_count':0})
        await self.store.put_record(RUN,'keyword_results','shard-00',{'items':[]})
        new_store = FirestoreStorage(self.client)
        self.assertEqual((await new_store.get_run(RUN))['status'], 'COMPLETED')
        self.assertEqual(await new_store.get_record(RUN,'keyword_results','shard-00'), {'items':[]})


class CompatibilityTests(unittest.TestCase):
    def test_legacy_ports_reexport_same_objects(self):
        from app.ports import storage
        from app.storage import base
        for symbol in ('AnalysisStorage', 'StorageError', 'RecordNotFound', 'RecordAlreadyExists', 'RecordKind'):
            self.assertIs(getattr(base, symbol), getattr(storage, symbol))
