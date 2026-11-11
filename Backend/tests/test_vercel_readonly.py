"""Offline security and deployment contracts; no cloud access."""
import base64
import os
import unittest
from unittest.mock import patch

import httpx

from app.main import create_app
from app.storage.fake import FakeStorage
from app.storage.vercel_identity import VercelCredentials, storage_for_request

PASSWORD = 'test-password-' + 'x' * 32
AUTH = 'Basic ' + base64.b64encode(('viewer:' + PASSWORD).encode()).decode()
RUN = 'a' * 32


class ReadOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_paths_protection_and_no_recovery(self):
        storage = FakeStorage()
        await storage.create_run(RUN, {'status': 'COMPLETED', 'keyword_count': 0})
        def factory(token):
            return storage
        with patch.dict(os.environ, {'TREND_SCOUT_READ_PASSWORD': PASSWORD}, clear=False), \
             patch('app.main.HostRunLock', side_effect=AssertionError('must not create lock')), \
             patch('app.main.RunService', side_effect=AssertionError('must not start runs')):
            app = create_app(storage=storage, read_only=True, read_storage_factory=factory)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    self.assertEqual((await client.get('/api/health')).status_code, 200)
                    self.assertEqual((await client.get('/api/runs/' + RUN)).status_code, 401)
                    self.assertEqual((await client.get('/dashboard/modules/api.js')).status_code, 401)
                    self.assertEqual((await client.post('/api/runs', headers={'Authorization': AUTH})).status_code, 405)
                    for path in ['/dashboard/', '/dashboard/app.js', '/dashboard/styles.css', '/dashboard/modules/api.js', '/dashboard/modules/i18n/en.js', '/api/runs/' + RUN, '/api/runs/' + RUN + '/keywords']:
                        response = await client.get(path, headers={'Authorization': AUTH})
                        self.assertEqual(response.status_code, 200, path)
                        self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertEqual((await client.get('/api/runs/' + RUN, headers={'Authorization': 'Basic invalid'})).status_code, 401)
                    self.assertEqual((await client.get('/dashboard/AGENTS.md', headers={'Authorization': AUTH})).status_code, 404)
                    self.assertEqual((await client.get('/dashboard/modules/../app/main.py', headers={'Authorization': AUTH})).status_code, 404)
                    self.assertTrue((await client.get('/api/config')).json()['read_only'])

    async def test_fail_closed_and_vercel_forces_read_only(self):
        with patch.dict(os.environ, {'VERCEL': '1', 'TREND_SCOUT_READ_PASSWORD': ''}, clear=False):
            app = create_app(read_only=False)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    self.assertEqual((await client.get('/api/runs/' + RUN)).status_code, 503)
                    self.assertEqual((await client.post('/api/runs')).status_code, 405)

    def test_oidc_identity_no_adc_fallback(self):
        with patch.dict(os.environ, {'GOOGLE_CLOUD_PROJECT': 'test-project', 'GOOGLE_WIF_AUDIENCE': '//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/p'}, clear=False):
            with self.assertRaises(ValueError):
                storage_for_request(None)
            with patch('app.storage.vercel_identity.firestore.Client') as client:
                storage_for_request('synthetic-oidc-token')
                credential = client.call_args.kwargs['credentials']
                self.assertIsInstance(credential, VercelCredentials)
                self.assertEqual(credential.retrieve_subject_token(None), 'synthetic-oidc-token')
                self.assertEqual(client.call_args.kwargs['database'], '(default)')

    def test_mocked_sts_exchange_uses_request_token(self):
        from urllib.parse import parse_qs
        audience = '//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/p'
        credentials = VercelCredentials(oidc_token='synthetic-token', audience=audience)
        class FakeResponse:
            status = 200
            data = b'{"access_token":"synthetic-access","expires_in":3600,"token_type":"Bearer"}'
        def fake_request(**kwargs):
            self.assertEqual(kwargs['url'], 'https://sts.googleapis.com/v1/token')
            body = parse_qs(kwargs['body'].decode())
            self.assertEqual(body['subject_token'], ['synthetic-token'])
            self.assertEqual(body['audience'], [audience])
            return FakeResponse()
        credentials.refresh(fake_request)
        self.assertEqual(credentials.token, 'synthetic-access')
