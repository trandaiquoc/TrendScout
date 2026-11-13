"""Offline dashboard structure and interaction regression."""

import unittest
from pathlib import Path

PUBLIC = Path(__file__).resolve().parents[2] / "Frontend" / "public"


class WorkspaceStructureTests(unittest.TestCase):
    def test_required_workspace_sections_and_safe_disabled_controls(self):
        html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        for name in (
            "overview",
            "collections",
            "sources",
            "runs",
            "algorithms",
            "quality",
            "research",
            "settings",
        ):
            with self.subTest(name=name):
                self.assertIn('data-view="' + name + '"', html)
        # The runtime source registry now owns live/synthetic status labels;
        # the static shell must expose the corresponding source section.
        self.assertIn('data-i18n="sources.active-title"', html)
        self.assertIn('role="tablist"', html)
        self.assertIn('data-dashboard-tab="analysis"', html)
        self.assertNotIn('data-view="keywords"', html)
        self.assertNotIn('data-view="refresh"', html)
        self.assertNotIn('data-view="usage"', html)

    def test_all_browser_http_access_uses_the_shared_client(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PUBLIC / "app.js", *sorted((PUBLIC / "modules").rglob("*.js")))
        )
        self.assertIn("method: 'GET'", source)
        self.assertEqual(source.count("fetch("), 1)
        self.assertEqual(source.count("Authorization"), 1)
        self.assertIn("path,{method,body,locale:getLocale(),headers:{Authorization:`Bearer ${token}`}}", source)
        self.assertIn("finally{if(key)key.value='';}", source)
        self.assertNotIn("localStorage.setItem('scheduler", source)
        self.assertNotIn("sessionStorage.setItem('scheduler", source)
        self.assertNotIn("YOUTUBE_API_KEY", source)
        self.assertNotIn("SERPAPI_API_KEY", source)
