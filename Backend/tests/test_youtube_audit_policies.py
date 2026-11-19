"""Policy pages describe the published version and current runtime guards."""

import unittest

import httpx

from app.demo_local import create_demo_app
from app.main import create_app


class PolicyPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_policy_pages_are_complete_relative_and_published(self):
        app = create_demo_app(test_fixture=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            homepage = await client.get("/dashboard/")
            privacy = await client.get("/dashboard/privacy.html")
            terms = await client.get("/dashboard/terms.html")
            css = await client.get("/dashboard/policy.css")
        self.assertEqual([homepage.status_code, privacy.status_code, terms.status_code,
                          css.status_code], [200, 200, 200, 200])
        self.assertIn('/dashboard/privacy.html', homepage.text)
        self.assertIn('/dashboard/terms.html', homepage.text)
        self.assertIn('POLICY PUBLISHED', homepage.text)
        self.assertIn('href="policy.css"', privacy.text)
        self.assertIn('href="https://trend-scout-mu.vercel.app/dashboard/terms.html"',
                      privacy.text)
        self.assertIn('href="https://trend-scout-mu.vercel.app/dashboard/privacy.html"',
                      terms.text)
        self.assertIn('d6-youtube-v2', privacy.text)
        self.assertNotIn('DRAFT', privacy.text + terms.text)
        self.assertNotIn('NOT PUBLISHED', privacy.text + terms.text)

        for response in (privacy, terms):
            self.assertIn('https://policies.google.com/privacy', response.text)
            self.assertIn('https://www.youtube.com/t/terms', response.text)
        self.assertIn('The API key is resolved by the server and is not sent to the browser',
                      privacy.text)
        self.assertIn('Cache freshness is eight hours, the cache retention ceiling is seven days',
                      privacy.text)
        self.assertIn('completed Fetch receipt/checkpoint is retained for up to one hour',
                      privacy.text)
        self.assertIn('Targeted YouTube deletion removes YouTube manifest entries', privacy.text)
        self.assertIn('No automatic backup job for the standalone SQLite databases', privacy.text)
        self.assertIn('Search-result position is not search volume', terms.text)
        self.assertIn('does not prove that creators, authors, viewers, or readers are located',
                      terms.text)
        self.assertIn('YouTube records are currently excluded from keyword extraction', terms.text)
        self.assertIn('Do not bypass source approvals', terms.text)
        self.assertIn('do not designate a governing-law jurisdiction', terms.text)
        self.assertIn('do not add a warranty, service-level', terms.text)
        self.assertIn('@media (max-width: 560px)', css.text)
        self.assertIn(':focus-visible', css.text)

    async def test_read_only_dashboard_serves_published_policy_without_enabling_fetch(self):
        app = create_app(read_only=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            policy = await client.get("/dashboard/privacy.html")
            capabilities = await client.get("/api/v1/capabilities")
            blocked = await client.post("/api/v1/fetches", json={})
        self.assertEqual(policy.status_code, 200)
        self.assertIn('d6-youtube-v2', policy.text)
        self.assertIn('Shared LIVE Fetch', policy.text)
        self.assertFalse(capabilities.json()["fetch_enabled"])
        self.assertEqual(blocked.status_code, 405)


if __name__ == "__main__":
    unittest.main()
