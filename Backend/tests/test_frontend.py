"""Same-origin dashboard smoke tests; no live provider or Firestore writes."""

import tempfile
import unittest
from pathlib import Path

import httpx

from app.main import create_app
from app.storage.fake import FakeStorage


class FrontendTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_static_and_health(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(storage=FakeStorage(), lock_path=str(Path(directory) / 'lock'))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    root = await client.get('/')
                    self.assertEqual(root.status_code, 307)
                    self.assertEqual(root.headers['location'], '/dashboard/')
                    page = await client.get('/dashboard/')
                    self.assertEqual(page.status_code, 200)
                    self.assertIn('data-theme="light"', page.text)
                    self.assertIn('id="open-fetch"', page.text)
                    self.assertNotIn('operator-token', page.text)
                    self.assertEqual((await client.get('/dashboard/app.js')).status_code, 200)
                    self.assertEqual((await client.get('/dashboard/styles.css')).status_code, 200)
                    self.assertEqual((await client.get('/dashboard/modules/api.js')).status_code, 200)
                    self.assertEqual((await client.get('/dashboard/modules/i18n/en.js')).status_code, 200)
                    self.assertEqual((await client.get('/api/health')).json(), {'status': 'ok'})
                    self.assertEqual((await client.post('/api/runs')).status_code, 503)

    async def test_frontend_mount_cannot_expose_repository_root(self):
        app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                self.assertEqual((await client.get('/dashboard/AGENTS.md')).status_code, 404)
