"""Policy pages describe current behavior and remain unpublished drafts."""

import unittest

import httpx

from app.demo_local import create_demo_app
from app.main import create_app


class PolicyPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_policy_pages_are_complete_relative_and_unapproved(self):
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
        self.assertIn('PENDING APPROVAL', homepage.text)
        self.assertIn('href="policy.css"', privacy.text)
        self.assertIn('href="terms.html"', privacy.text)
        self.assertIn('href="privacy.html"', terms.text)
        self.assertNotIn('href="/dashboard/', privacy.text + terms.text)
        self.assertIn('DRAFT', privacy.text)
        self.assertIn('NOT PUBLISHED', privacy.text)
        self.assertIn('DRAFT', terms.text)
        self.assertIn('NOT PUBLISHED', terms.text)

        for response in (privacy, terms):
            self.assertIn('[OWNER REVIEW REQUIRED:', response.text)
            self.assertIn('To be set after approval and publication', response.text)
            self.assertIn('https://policies.google.com/privacy', response.text)
            self.assertIn('https://www.youtube.com/t/terms', response.text)
        self.assertNotIn('tdquoc02@gmail.com', privacy.text + terms.text)
        self.assertNotIn('Trần Đại Quốc', privacy.text + terms.text)
        self.assertIn('no live YouTube API request', privacy.text)
        self.assertIn('eight-hour cache freshness window', privacy.text)
        self.assertIn('at most seven days of cache retention', privacy.text)
        self.assertIn('one hour for a completed Fetch receipt/checkpoint', privacy.text)
        self.assertIn('A targeted deletion removes YouTube manifest entries', privacy.text)
        self.assertIn('cannot remove information held by YouTube', privacy.text)
        self.assertIn('retention period for those receipts remains an owner decision', privacy.text)
        self.assertIn('No automatic backup job', privacy.text)
        self.assertIn('Search position is not search volume', terms.text)
        self.assertIn('does not prove that creators or viewers are located', terms.text)
        self.assertIn('YouTube API Services', terms.text)
        self.assertIn('Do not attempt to bypass', terms.text)
        self.assertIn('does not publish or operate the local backend', terms.text)
        self.assertIn('governing law, dispute handling, liability language', terms.text)
        self.assertIn('@media (max-width: 560px)', css.text)
        self.assertIn(':focus-visible', css.text)

    async def test_read_only_dashboard_serves_draft_without_enabling_fetch(self):
        app = create_app(read_only=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            policy = await client.get("/dashboard/privacy.html")
            blocked = await client.post("/api/v1/fetches", json={})
        self.assertEqual(policy.status_code, 200)
        self.assertIn('DRAFT', policy.text)
        self.assertEqual(blocked.status_code, 405)


if __name__ == "__main__":
    unittest.main()
