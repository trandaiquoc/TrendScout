"""Offline composition and dependency-boundary contracts."""
import asyncio
import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from app import composition
from app.main import create_app
from app.orchestration.runs import HostRunLock, RunService
from app.storage.fake import FakeStorage
from test_runs import fixture


class FakeSource:
    calls = 0

    async def collect(self):
        FakeSource.calls += 1
        return fixture()


class CompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_fake_source_storage_and_pipeline(self):
        FakeSource.calls = 0
        store = FakeStorage()
        with tempfile.TemporaryDirectory() as directory:
            clean = Mock(wraps=composition.clean_snapshot)
            extract = Mock(wraps=composition.extract_keywords)
            score = Mock(wraps=composition.score_trends)
            with patch.object(composition, 'clean_snapshot', clean), \
                 patch.object(composition, 'extract_keywords', extract), \
                 patch.object(composition, 'score_trends', score), \
                 patch.object(composition.FirestoreStorage, 'from_environment', side_effect=AssertionError('ADC used')), \
                 patch.object(composition, 'WikimediaAdapter', side_effect=AssertionError('real adapter used')):
                service = composition.build_run_service(
                    storage=store, adapter_factory=FakeSource, lock_path=str(Path(directory)/'lock'),
                    mode='synthetic', lock_factory=HostRunLock, service_factory=RunService)
                self.assertEqual(FakeSource.calls, 0)
                run_id = await service.start()
                for _ in range(200):
                    run = await store.get_run(run_id)
                    if run['status'] in ('COMPLETED', 'PARTIAL', 'FAILED', 'ABORTED'):
                        break
                    await asyncio.sleep(.005)
                self.assertEqual(run['status'], 'PARTIAL')
                self.assertEqual(FakeSource.calls, 1)
                self.assertEqual(clean.call_count, 8)
                self.assertEqual(extract.call_count, 8)
                score.assert_called_once()
                await service.shutdown()

    async def test_read_only_no_service_or_firestore_initialization_and_old_run(self):
        old_run = '30e96cf96abe48dbb304fb9808c89873'
        store = FakeStorage()
        golden = json.loads((Path(__file__).parent / 'fixtures/golden_v1/expected.json').read_text())
        row = dict(golden['entities'][0], search_keyword='fashion week')
        await store.create_run(old_run, {'run_id': old_run, 'status':'COMPLETED', 'keyword_count':1})
        await store.put_record(old_run, 'keyword_results', f"shard-{int(row['keyword_id'][0], 16):02d}", {'items':[row]})
        password = 'synthetic-password-' + 'x'*32
        with patch.dict('os.environ', {'VERCEL':'1', 'TREND_SCOUT_READ_PASSWORD':password}), \
             patch('app.main.build_run_service', side_effect=AssertionError('service initialized')), \
             patch.object(composition.FirestoreStorage, 'from_environment', side_effect=AssertionError('ADC used')), \
             patch.object(composition, 'WikimediaAdapter', side_effect=AssertionError('source created')):
            app = create_app(read_storage_factory=lambda _: store)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', auth=('viewer', password)) as client:
                    self.assertEqual((await client.get('/api/health')).status_code, 200)
                    self.assertEqual((await client.get('/api/runs/' + old_run)).json()['run_id'], old_run)
                    path = '/api/runs/' + old_run + '/keywords'
                    self.assertEqual((await client.get(path)).json()['items'][0]['keyword_id'], row['keyword_id'])
                    self.assertEqual((await client.get(path + '/' + row['keyword_id'])).json()['score'], 100)
                    self.assertEqual((await client.post('/api/runs')).status_code, 405)

    async def test_shutdown_cancels_injected_source_and_records_aborted(self):
        started = asyncio.Event()
        class BlockingSource:
            async def collect(self):
                started.set()
                await asyncio.Event().wait()
        store = FakeStorage()
        with tempfile.TemporaryDirectory() as directory:
            service = composition.build_run_service(storage=store, adapter_factory=BlockingSource,
                lock_path=str(Path(directory)/'lock'), mode='synthetic',
                lock_factory=HostRunLock, service_factory=RunService)
            run_id = await service.start()
            await asyncio.wait_for(started.wait(), timeout=1)
            await service.shutdown()
            self.assertEqual((await store.get_run(run_id))['status'], 'ABORTED')

    async def test_source_failure_marks_failed(self):
        class BrokenSource:
            async def collect(self):
                raise RuntimeError('synthetic source failure')
        store = FakeStorage()
        with tempfile.TemporaryDirectory() as directory:
            service = composition.build_run_service(storage=store, adapter_factory=BrokenSource,
                lock_path=str(Path(directory)/'lock'), mode='synthetic',
                lock_factory=HostRunLock, service_factory=RunService)
            run_id = await service.start()
            for _ in range(200):
                run = await store.get_run(run_id)
                if run['status'] in ('FAILED','PARTIAL','COMPLETED','ABORTED'):
                    break
                await asyncio.sleep(.005)
            self.assertEqual(run['status'], 'FAILED')
            self.assertIn('synthetic source failure', run['error'])
            await service.shutdown()


class ImportBoundaryTests(unittest.TestCase):
    def test_models_and_source_port_do_not_import_sdk_or_http_client(self):
        backend = Path(__file__).resolve().parents[1]
        files = list((backend / 'app' / 'models').glob('*.py')) + [backend / 'app' / 'adapters' / 'base.py']
        for file in files:
            tree = ast.parse(file.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [x.name for x in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or '']
                else:
                    continue
                for name in names:
                    with self.subTest(file=file.name, module=name):
                        self.assertFalse(name.startswith(('firebase_admin', 'google.cloud', 'httpx', 'requests')))

    def test_use_case_does_not_import_concrete_pipeline_or_source(self):
        file = Path(__file__).resolve().parents[1] / 'app' / 'orchestration' / 'runs.py'
        tree = ast.parse(file.read_text(encoding='utf-8'))
        imports = [n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertFalse(any(x.startswith(('app.adapters.wikimedia', 'app.analysis', 'app.processing', 'app.storage.firestore')) for x in imports))
