"""Structural checks for the persisted Collections and Keyword Workbench."""

import unittest
from pathlib import Path

PUBLIC = Path(__file__).resolve().parents[2] / "Frontend" / "public"


class KeywordWorkbenchUITests(unittest.TestCase):
    def test_collections_is_the_persisted_workbench_route(self):
        html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        app = (PUBLIC / "app.js").read_text(encoding="utf-8")
        module = (PUBLIC / "modules" / "keyword-workbench.js").read_text(encoding="utf-8")
        self.assertIn('id="workbench-root"', html)
        self.assertIn("name === 'results' || name === 'keywords'", app)
        self.assertIn("legacyOverview ? 'collections'", app)
        for contract in (
            "/api/v1/datasets/latest/keywords?",
            "/api/v1/collections/keywords/",
            "/api/v1/collections/bulk",
            "all_filtered:state.allFiltered",
            "date_from",
            "classification",
            "evidence",
            "subtree_unique_keyword_count",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, module)

    def test_workbench_locales_and_responsive_tree_are_present(self):
        english = (PUBLIC / "modules" / "i18n" / "en.js").read_text(encoding="utf-8")
        vietnamese = (PUBLIC / "modules" / "i18n" / "vi.js").read_text(encoding="utf-8")
        css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
        self.assertIn("'workbench.all-keywords'", english)
        self.assertIn("'workbench.all-keywords'", vietnamese)
        self.assertIn("aria-expanded", (PUBLIC / "modules" / "keyword-workbench.js").read_text(encoding="utf-8"))
        self.assertIn(".workbench-sidebar.is-open", css)
        self.assertIn("@media (max-width: 600px)", css)

    def test_advanced_filters_are_collapsed_and_user_copy_avoids_model_jargon(self):
        module = (PUBLIC / "modules" / "keyword-workbench.js").read_text(encoding="utf-8")
        english = (PUBLIC / "modules" / "i18n" / "en.js").read_text(encoding="utf-8")
        vietnamese = (PUBLIC / "modules" / "i18n" / "vi.js").read_text(encoding="utf-8")
        self.assertIn("workbench-advanced-filters", module)
        self.assertIn("workbench.more-filters", module)
        self.assertNotIn("AI CỤC BỘ", vietnamese)
        self.assertIn("'workbench.auto-assigned': 'Tự động sắp xếp'", vietnamese)
        self.assertIn("'workbench.in-collections': 'Đã có trong Bộ sưu tập'", vietnamese)
        self.assertIn("value===null||value===undefined", module)
        self.assertIn("sequence!==state.collectionSequence", module)
        self.assertIn("workbench.summary-unavailable", module)
        self.assertIn("workbench.rows-unavailable", module)
        self.assertIn("Number.isInteger(result.total)&&result.total>=0?result.total:null", module)
        self.assertNotIn("state.total=result.total||0", module)
        self.assertIn("'workbench.rows-unavailable'", english)
        self.assertIn("'workbench.rows-unavailable'", vietnamese)

    def test_collection_tree_keeps_scroll_and_focus_when_it_renders_again(self):
        module = (PUBLIC / "modules" / "keyword-workbench.js").read_text(encoding="utf-8")
        css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
        self.assertIn("const scrollTop=sidebar?.scrollTop??0", module)
        self.assertIn("sidebar.scrollTop=scrollTop", module)
        self.assertIn("replacement?.focus({preventScroll:true})", module)
        self.assertIn("toggle.focus({preventScroll:true})", module)
        self.assertIn("window.scrollTo(pageX,pageY)", module)
        self.assertIn("border-left: 1px solid var(--line)", css)


if __name__ == "__main__":
    unittest.main()
