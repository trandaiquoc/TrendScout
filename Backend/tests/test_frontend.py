"""Same-origin dashboard smoke tests; no live provider or Firestore writes."""

import tempfile
import unittest
from pathlib import Path

import httpx

from app.main import create_app
from app.storage.fake import FakeStorage


class FrontendTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_routes_root_assets_and_health(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(storage=FakeStorage(), lock_path=str(Path(directory) / 'lock'))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    root = await client.get('/')
                    self.assertEqual(root.status_code, 200)
                    page = root
                    self.assertEqual(page.status_code, 200)
                    self.assertIn('data-theme="light"', page.text)
                    self.assertIn('id="open-fetch"', page.text)
                    self.assertIn('id="keyword-collection-filter-toggle"', page.text)
                    self.assertIn('value="non_topic" data-i18n="dataset.state-non-topic"', page.text)
                    self.assertNotIn('operator-token', page.text)
                    self.assertIn('href="/styles.css"', page.text)
                    self.assertNotIn('href="/dashboard', page.text)
                    self.assertEqual((await client.get('/app.js')).status_code, 200)
                    self.assertEqual((await client.get('/styles.css')).status_code, 200)
                    self.assertEqual((await client.get('/modules/api.js')).status_code, 200)
                    self.assertEqual((await client.get('/modules/i18n/en.js')).status_code, 200)
                    for route in ('/keywords', '/collections', '/products', '/trends', '/sources', '/research', '/settings', '/runs', '/algorithms', '/quality'):
                        with self.subTest(route=route):
                            response = await client.get(route)
                            self.assertEqual(response.status_code, 200)
                            self.assertIn('id="app-shell"', response.text)
                    for route in ('/collections/collection-a', '/sources/google_web_serp'):
                        with self.subTest(route=route):
                            response = await client.get(route)
                            self.assertEqual(response.status_code, 200)
                            self.assertIn('id="app-shell"', response.text)
                    self.assertEqual((await client.get('/privacy')).status_code, 200)
                    self.assertEqual((await client.get('/terms')).status_code, 200)
                    legacy = await client.get('/dashboard/', follow_redirects=False)
                    self.assertEqual(legacy.status_code, 308)
                    self.assertEqual(legacy.headers['location'], '/')
                    legacy_asset = await client.get('/dashboard/app.js', follow_redirects=False)
                    self.assertEqual(legacy_asset.status_code, 308)
                    self.assertEqual(legacy_asset.headers['location'], '/app.js')
                    self.assertEqual((await client.get('/api/health')).json(), {'status': 'ok'})
                    self.assertEqual((await client.post('/api/runs')).status_code, 503)

    async def test_frontend_mount_cannot_expose_repository_root(self):
        app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                self.assertEqual((await client.get('/AGENTS.md')).status_code, 404)
