"""Offline security and deployment contracts; no cloud access."""
import base64
import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import httpx
from google.api_core.exceptions import PermissionDenied
from google.auth.exceptions import RefreshError

from app.main import create_app
from app.ports.storage import StorageError
from app.storage.fake import FakeStorage
from app.storage.firestore import FirestoreStorage
from app.storage.vercel_identity import (
    FIRESTORE_PROJECT_ID,
    VERCEL_IMPERSONATED_SERVICE_ACCOUNT,
    VERCEL_OIDC_ISSUER,
    VERCEL_OIDC_SUBJECT,
    VERCEL_OIDC_TOKEN_AUDIENCE,
    WIF_PROVIDER_AUDIENCE,
    VercelCredentials,
    storage_for_request,
)

PASSWORD = 'test-password-' + 'x' * 32
AUTH = 'Basic ' + base64.b64encode(('viewer:' + PASSWORD).encode()).decode()
RUN = 'a' * 32


def vercel_oidc_token(*, issuer=VERCEL_OIDC_ISSUER, audience=VERCEL_OIDC_TOKEN_AUDIENCE,
                      subject=VERCEL_OIDC_SUBJECT, expires_at=None):
    """Build an unsigned synthetic token for rejection-only local claim tests."""
    expires_at = int(time.time()) + 3600 if expires_at is None else expires_at

    def encode(value):
        raw = json.dumps(value, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()

    header = encode({'alg': 'none', 'typ': 'JWT'})
    claims = {'iss': issuer, 'aud': audience, 'sub': subject, 'exp': expires_at}
    return f'{header}.{encode(claims)}.synthetic-signature'


def dashboard_bundle_module():
    public = Path(__file__).resolve().parents[2] / 'Frontend' / 'public'
    module = ModuleType('app.dashboard_assets')
    module.ASSETS = {
        path.relative_to(public).as_posix(): path.read_bytes()
        for path in sorted(public.rglob('*'))
        if path.is_file() and not any(part.startswith('.') for part in path.relative_to(public).parts)
    }
    return module


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
            with patch.dict(sys.modules, {'app.dashboard_assets': dashboard_bundle_module()}):
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                        self.assertEqual((await client.get('/')).status_code, 307)
                        self.assertEqual((await client.get('/')).headers['location'], '/dashboard/')
                        self.assertEqual((await client.get('/api/health')).status_code, 200)
                        self.assertEqual((await client.get('/api/runs/' + RUN)).status_code, 401)
                        self.assertEqual((await client.get('/api/v1/capabilities')).status_code, 200)
                        for path in ['/dashboard/', '/dashboard/app.js', '/dashboard/styles.css', '/dashboard/modules/api.js', '/dashboard/modules/i18n/en.js']:
                            response = await client.get(path)
                            self.assertEqual(response.status_code, 200, path)
                        self.assertEqual((await client.post('/api/runs', headers={'Authorization': AUTH})).status_code, 405)
                        self.assertEqual((await client.post('/api/v1/fetches', content='{}')).status_code, 405)
                        for path in ['/api/runs/' + RUN, '/api/runs/' + RUN + '/keywords']:
                            response = await client.get(path, headers={'Authorization': AUTH})
                            self.assertEqual(response.status_code, 200, path)
                            self.assertEqual(response.headers['cache-control'], 'no-store')
                        self.assertEqual((await client.get('/api/runs/' + RUN, headers={'Authorization': 'Basic invalid'})).status_code, 401)
                        self.assertEqual((await client.get('/dashboard/AGENTS.md')).status_code, 404)
                        self.assertEqual((await client.get('/dashboard/modules/../app/main.py')).status_code, 404)
                        self.assertTrue((await client.get('/api/config')).json()['read_only'])

    async def test_public_dashboard_needs_no_read_password_but_run_reads_fail_closed(self):
        read_storage_factory = Mock(side_effect=AssertionError('private storage read'))
        with patch.dict(os.environ, {'VERCEL': '1'}, clear=True), \
             patch.dict(sys.modules, {'app.dashboard_assets': dashboard_bundle_module()}):
            app = create_app(read_storage_factory=read_storage_factory)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    self.assertEqual((await client.get('/')).status_code, 307)
                    self.assertEqual((await client.get('/')).headers['location'], '/dashboard/')
                    self.assertEqual((await client.get('/dashboard/')).status_code, 200)
                    self.assertEqual((await client.get('/dashboard/styles.css')).status_code, 200)
                    self.assertEqual((await client.get('/dashboard/modules/i18n/en.js')).status_code, 200)
                    self.assertEqual((await client.get('/api/health')).status_code, 200)
                    self.assertEqual((await client.get('/api/v1/capabilities')).status_code, 200)
                    self.assertEqual((await client.get('/api/runs/' + RUN)).status_code, 503)
                    self.assertEqual((await client.post('/api/runs')).status_code, 405)
                    self.assertEqual((await client.post('/api/v1/fetches', content='{}')).status_code, 405)
            read_storage_factory.assert_not_called()

    def test_oidc_identity_no_adc_fallback(self):
        env = {
            'GOOGLE_CLOUD_PROJECT': FIRESTORE_PROJECT_ID,
            'GOOGLE_WIF_AUDIENCE': WIF_PROVIDER_AUDIENCE,
        }
        token = vercel_oidc_token()
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                storage_for_request(None)
            with patch('app.storage.vercel_identity.firestore.Client') as client:
                storage = storage_for_request(token)
                credential = client.call_args.kwargs['credentials']
                self.assertIsInstance(credential, VercelCredentials)
                self.assertEqual(
                    credential._source_credentials.retrieve_subject_token(None), token
                )
                self.assertNotIn('oidc_token', credential._source_credentials.info)
                self.assertNotIn(token, repr(credential._source_credentials.info))
                self.assertEqual(credential.service_account_email, VERCEL_IMPERSONATED_SERVICE_ACCOUNT)
                self.assertEqual(client.call_args.kwargs['database'], '(default)')
                self.assertEqual(client.call_args.kwargs['project'], FIRESTORE_PROJECT_ID)
                self.assertIsInstance(storage, FirestoreStorage)

    def test_rejects_wrong_issuer_audience_and_nonproduction_subject(self):
        env = {
            'GOOGLE_CLOUD_PROJECT': FIRESTORE_PROJECT_ID,
            'GOOGLE_WIF_AUDIENCE': WIF_PROVIDER_AUDIENCE,
        }
        invalid_tokens = (
            (vercel_oidc_token(issuer='https://attacker.invalid'), 'issuer'),
            (vercel_oidc_token(audience='https://vercel.com/other-team'), 'audience'),
            (vercel_oidc_token(subject='owner:rakoon1:project:trend-scout:environment:preview'),
             'subject'),
        )
        with patch.dict(os.environ, env, clear=True):
            for token, expected_error in invalid_tokens:
                with self.subTest(expected_error=expected_error):
                    with patch('app.storage.vercel_identity.firestore.Client') as client:
                        with self.assertRaisesRegex(ValueError, expected_error):
                            storage_for_request(token)
                        client.assert_not_called()

    def test_rejects_wrong_project_and_wrong_provider_audience(self):
        token = vercel_oidc_token()
        with patch('app.storage.vercel_identity.firestore.Client') as client:
            with self.assertRaisesRegex(ValueError, 'project'):
                storage_for_request(
                    token,
                    project_id='wrong-project',
                    audience=WIF_PROVIDER_AUDIENCE,
                )
            with self.assertRaisesRegex(ValueError, 'audience'):
                storage_for_request(
                    token,
                    project_id=FIRESTORE_PROJECT_ID,
                    audience='//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/other/providers/other',
                )
            client.assert_not_called()

    def test_rejects_expired_and_malformed_tokens(self):
        expired = vercel_oidc_token(expires_at=int(time.time()) - 1)
        for token in (expired, 'not-a-jwt'):
            with self.subTest(token=token[:8]):
                with self.assertRaises(ValueError):
                    VercelCredentials(oidc_token=token, audience=WIF_PROVIDER_AUDIENCE)

    def test_missing_project_or_audience_fails_without_adc_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch('app.storage.vercel_identity.firestore.Client') as client:
                with patch.object(
                    FirestoreStorage, 'from_project', side_effect=AssertionError('ADC fallback')
                ) as from_project:
                    with self.assertRaisesRegex(ValueError, 'project'):
                        storage_for_request(vercel_oidc_token())
                    with self.assertRaisesRegex(ValueError, 'audience'):
                        storage_for_request(
                            vercel_oidc_token(),
                            project_id=FIRESTORE_PROJECT_ID,
                            audience=None,
                        )
                    client.assert_not_called()
                    from_project.assert_not_called()

    def test_mocked_sts_exchange_impersonates_the_intended_service_account(self):
        token = vercel_oidc_token()
        credentials = VercelCredentials(oidc_token=token, audience=WIF_PROVIDER_AUDIENCE)
        calls = []

        class FakeResponse:
            def __init__(self, status, value):
                self.status = status
                self.data = json.dumps(value).encode()

        def fake_request(**kwargs):
            calls.append(kwargs)
            if kwargs['url'] == 'https://sts.googleapis.com/v1/token':
                body = parse_qs(kwargs['body'].decode())
                self.assertEqual(body['subject_token'], [token])
                self.assertEqual(body['audience'], [WIF_PROVIDER_AUDIENCE])
                return FakeResponse(200, {
                    'access_token': 'synthetic-sts-access',
                    'expires_in': 3600,
                    'token_type': 'Bearer',
                })
            self.assertEqual(
                kwargs['url'],
                'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/'
                f'{VERCEL_IMPERSONATED_SERVICE_ACCOUNT}:generateAccessToken',
            )
            headers = {key.lower(): value for key, value in kwargs['headers'].items()}
            self.assertEqual(headers['authorization'], 'Bearer synthetic-sts-access')
            body = json.loads(kwargs['body'].decode())
            self.assertEqual(body['scope'], ['https://www.googleapis.com/auth/datastore'])
            self.assertEqual(body['lifetime'], '3600s')
            expiry = (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1))
            return FakeResponse(200, {
                'accessToken': 'synthetic-impersonated-access',
                'expireTime': expiry.strftime('%Y-%m-%dT%H:%M:%SZ'),
            })

        credentials.refresh(fake_request)
        self.assertEqual(credentials.token, 'synthetic-impersonated-access')
        self.assertEqual(len(calls), 2)

    def test_failed_service_account_impersonation_fails_closed(self):
        credentials = VercelCredentials(
            oidc_token=vercel_oidc_token(),
            audience=WIF_PROVIDER_AUDIENCE,
        )
        calls = []

        class FakeResponse:
            def __init__(self, status, value):
                self.status = status
                self.data = json.dumps(value).encode()

        def fake_request(**kwargs):
            calls.append(kwargs['url'])
            if kwargs['url'] == 'https://sts.googleapis.com/v1/token':
                return FakeResponse(200, {
                    'access_token': 'synthetic-sts-access',
                    'expires_in': 3600,
                    'token_type': 'Bearer',
                })
            return FakeResponse(403, {
                'error': {'code': 403, 'status': 'PERMISSION_DENIED', 'message': 'denied'}
            })

        with self.assertRaises(RefreshError):
            credentials.refresh(fake_request)
        self.assertEqual(len(calls), 2)
        self.assertIn('iamcredentials.googleapis.com', calls[-1])

    async def test_firestore_access_denied_propagates_without_adc_fallback(self):
        class DeniedDocument:
            def get(self):
                raise PermissionDenied('synthetic denial')

        class DeniedCollection:
            def document(self, _document_id):
                return DeniedDocument()

        class DeniedClient:
            def collection(self, _collection_name):
                return DeniedCollection()

        env = {
            'GOOGLE_CLOUD_PROJECT': FIRESTORE_PROJECT_ID,
            'GOOGLE_WIF_AUDIENCE': WIF_PROVIDER_AUDIENCE,
        }
        with patch.dict(os.environ, env, clear=True):
            with patch(
                'app.storage.vercel_identity.firestore.Client', return_value=DeniedClient()
            ) as client:
                with patch.object(
                    FirestoreStorage, 'from_project', side_effect=AssertionError('ADC fallback')
                ) as from_project:
                    storage = storage_for_request(vercel_oidc_token())
                    with self.assertRaises(StorageError):
                        await storage.get_run(RUN)
                    credentials = client.call_args.kwargs['credentials']
                    self.assertIsInstance(credentials, VercelCredentials)
                    self.assertEqual(
                        credentials.service_account_email,
                        VERCEL_IMPERSONATED_SERVICE_ACCOUNT,
                    )
                    client.assert_called_once()
                    from_project.assert_not_called()
