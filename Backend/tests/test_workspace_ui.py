"""Offline dashboard structure and interaction regression."""

import unittest
from pathlib import Path

PUBLIC = Path(__file__).resolve().parents[2] / "Frontend" / "public"


class WorkspaceStructureTests(unittest.TestCase):
    def test_dashboard_cached_detail_disabled_explanation_is_localized(self):
        app = (PUBLIC / "app.js").read_text(encoding="utf-8")
        english = (PUBLIC / "modules" / "i18n" / "en.js").read_text(encoding="utf-8")
        vietnamese = (PUBLIC / "modules" / "i18n" / "vi.js").read_text(encoding="utf-8")
        self.assertIn("button.title = t('table.detail-unavailable')", app)
        self.assertIn("'table.detail-unavailable': 'This saved keyword is not linked to a run with viewable details.'", english)
        self.assertIn("'table.detail-unavailable': 'Từ khóa đã lưu này chưa liên kết với lần chạy có thể xem chi tiết.'", vietnamese)
        self.assertNotIn("Detailed run evidence is unavailable for this cached ledger row.", app)

    def test_algorithm_catalog_loading_and_empty_states_are_localized(self):
        app = (PUBLIC / "app.js").read_text(encoding="utf-8")
        english = (PUBLIC / "modules" / "i18n" / "en.js").read_text(encoding="utf-8")
        vietnamese = (PUBLIC / "modules" / "i18n" / "vi.js").read_text(encoding="utf-8")
        self.assertIn("t('algorithms.loading-catalog')", app)
        self.assertIn("t('algorithms.filter-empty')", app)
        self.assertIn("'algorithms.loading-catalog': 'Loading analysis methods…'", english)
        self.assertIn("'algorithms.filter-empty': 'No analysis methods match these filters.'", english)
        self.assertIn("'algorithms.loading-catalog': 'Đang tải các phương pháp phân tích…'", vietnamese)
        self.assertIn("'algorithms.filter-empty': 'Không có phương pháp phân tích nào phù hợp với bộ lọc này.'", vietnamese)

    def test_algorithm_source_filter_uses_user_facing_source_names(self):
        app = (PUBLIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("sources.map((value) => new Option(localizedSourceName(value), value))", app)
        self.assertNotIn("new Option(value.replaceAll('_',' '), value)", app)

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
        self.assertIn('id="quality-coverage" data-i18n="quality.not-measured"', html)
        self.assertIn('id="quality-fetched" data-i18n="common.unavailable"', html)
        self.assertNotIn('id="quality-coverage">NOT_MEASURED', html)
        self.assertNotIn('id="quality-fetched">NOT_AVAILABLE', html)
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
