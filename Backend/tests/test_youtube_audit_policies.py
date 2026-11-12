"""The local policy drafts must be reachable and remain visibly incomplete."""

import unittest

import httpx

from app.demo_local import create_demo_app
from app.main import create_app


class PolicyPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_policy_links_and_unapproved_status(self):
        app = create_demo_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            homepage = await client.get("/dashboard/")
            privacy = await client.get("/dashboard/privacy.html")
            terms = await client.get("/dashboard/terms.html")
        self.assertEqual([homepage.status_code, privacy.status_code, terms.status_code], [200, 200, 200])
        self.assertIn('/dashboard/privacy.html', homepage.text)
        self.assertIn('/dashboard/terms.html', homepage.text)
        self.assertIn('PENDING APPROVAL', homepage.text)
        for response in (privacy, terms):
            self.assertIn('LOCAL DRAFT / NOT YET PUBLIC', response.text)
            self.assertIn('[CONTACT METHOD TO BE PROVIDED]', response.text)
            self.assertIn('https://policies.google.com/privacy', response.text)
            self.assertIn('https://www.youtube.com/t/terms', response.text)
        self.assertIn('[RETENTION PERIOD AND DELETION PROCESS TO BE PROVIDED]', privacy.text)
        self.assertIn('no YouTube-derived metrics are approved', privacy.text)

    async def test_read_only_dashboard_serves_policy_without_enable_fetch(self):
        app = create_app(read_only=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            policy = await client.get("/dashboard/privacy.html")
            blocked = await client.post("/api/v1/fetches", json={})
        self.assertEqual(policy.status_code, 200)
        self.assertIn('LOCAL DRAFT / NOT YET PUBLIC', policy.text)
        self.assertEqual(blocked.status_code, 405)


if __name__ == "__main__":
    unittest.main()
