"""Offline dashboard structure and interaction regression."""

import unittest
from pathlib import Path

PUBLIC = Path(__file__).resolve().parents[2] / "Frontend" / "public"


class WorkspaceStructureTests(unittest.TestCase):
    def test_required_workspace_sections_and_safe_disabled_controls(self):
        html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        for name in (
            "overview",
            "sources",
            "runs",
            "keywords",
            "algorithms",
            "quality",
            "refresh",
            "research",
            "usage",
            "settings",
        ):
            with self.subTest(name=name):
                self.assertIn('data-view="' + name + '"', html)
        self.assertIn('data-i18n="common.synthetic"', html)

    def test_all_browser_http_access_uses_the_shared_client(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PUBLIC / "app.js", *sorted((PUBLIC / "modules").rglob("*.js")))
        )
        self.assertIn("method: 'GET'", source)
        self.assertEqual(source.count("fetch("), 1)
        self.assertNotIn("Authorization", source)
        self.assertNotIn("YOUTUBE_API_KEY", source)
        self.assertNotIn("SERPAPI_API_KEY", source)
