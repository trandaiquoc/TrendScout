"""D2 dashboard contracts; no live provider or credential access."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "Frontend" / "public"


class D2DashboardContractTests(unittest.TestCase):
    @staticmethod
    def _theme_tokens(theme):
        css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
        selector = (
            r":root,\s*:root\[data-theme=\"light\"\]"
            if theme == "light"
            else r":root\[data-theme=\"dark\"\]"
        )
        match = re.search(selector + r"\s*\{([^}]+)\}", css)
        if not match:
            raise AssertionError(f"Missing {theme} theme token block")
        return dict(re.findall(r"(--[\w-]+):\s*(#[\da-fA-F]{6})", match.group(1)))

    @staticmethod
    def _contrast_ratio(foreground, background):
        def luminance(color):
            channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [
                channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                for channel in channels
            ]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    def test_workspace_has_persistent_theme_locale_and_ten_views(self):
        html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<html lang="vi"', html)
        self.assertIn('data-theme="light"', html)
        self.assertIn("data-theme-toggle", html)
        self.assertIn("data-locale-toggle", html)
        self.assertEqual(html.count("data-nav="), 10)
        for view in (
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
            with self.subTest(view=view):
                self.assertIn(f'data-view="{view}"', html)

    def test_locale_catalogs_are_separate_and_all_primary_copy_is_keyed(self):
        html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        app = (PUBLIC / "app.js").read_text(encoding="utf-8")
        template_keys = set(re.findall(r'data-i18n(?:-placeholder|-aria)?="([^"]+)"', html))
        dynamic_keys = set(
            re.findall(
                r"['\"]((?:a11y|algorithms|common|connection|detail|error|fetch|filter|keywords|"
                r"locale|nav|overview|quality|run|runs|settings|source|sources|status|table|"
                r"theme|time|sort|refresh|research|usage)\.[^'\"]+)['\"]",
                app,
            )
        )
        catalog_keysets = []
        required_keys = template_keys | dynamic_keys
        self.assertTrue(required_keys)
        for locale in ("vi", "en"):
            catalog_path = PUBLIC / "modules" / "i18n" / (locale + ".js")
            self.assertTrue(catalog_path.is_file())
            catalog = catalog_path.read_text(encoding="utf-8")
            catalog_keys = set(re.findall(r"^\s*'([^']+)':", catalog, re.MULTILINE))
            catalog_keysets.append(catalog_keys)
            self.assertEqual(required_keys - catalog_keys, set(), locale)
        self.assertEqual(catalog_keysets[0], catalog_keysets[1])

    def test_public_fetch_has_no_account_or_credential_flow_and_starts_disabled(self):
        html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PUBLIC / "app.js", *sorted((PUBLIC / "modules").rglob("*.js")))
        )
        self.assertNotRegex(html.lower(), r"\b(login|logout|register)\b")
        self.assertNotIn('type="password"', html.lower())
        self.assertNotIn("Authorization", source)
        self.assertIn('id="open-fetch"', html)
        self.assertIn(
            "disabled", html[html.index('id="open-fetch"') : html.index('id="open-fetch"') + 300]
        )
        self.assertIn("/api/v1/fetches", source)
        self.assertIn("Idempotency-Key", source)
        self.assertNotIn("operator-token", html + source)

    def test_source_cards_are_truthful_for_all_four_requested_sources(self):
        html = (PUBLIC / "index.html").read_text(encoding="utf-8")
        for source in ("wikimedia", "youtube", "google-search", "bing-search"):
            with self.subTest(source=source):
                self.assertIn(f'data-source="{source}"', html)
        self.assertIn('data-source-status="adapter-implemented-live-unverified"', html)
        self.assertGreaterEqual(html.count('data-source-status="not-configured"'), 3)

    def test_theme_status_and_secondary_text_tokens_meet_aa_contrast(self):
        for theme in ("light", "dark"):
            tokens = self._theme_tokens(theme)
            text_pairs = (
                ("--ink", "--surface"),
                ("--muted", "--surface"),
                ("--muted-soft", "--surface-soft"),
                ("--accent", "--surface"),
                ("--accent-strong", "--accent-soft"),
                ("--teal", "--teal-soft"),
                ("--amber", "--amber-soft"),
                ("--red", "--red-soft"),
                ("--green", "--green-soft"),
            )
            for foreground, background in text_pairs:
                with self.subTest(theme=theme, foreground=foreground):
                    self.assertGreaterEqual(
                        self._contrast_ratio(tokens[foreground], tokens[background]),
                        4.5,
                    )


if __name__ == "__main__":
    unittest.main()
