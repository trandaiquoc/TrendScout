"""Shared conservative keyword identity normalization."""

import unittest

from app.analysis.keyword_identity import canonical_keyword_digest, normalize_keyword_text
from app.sources.google_trends.trending_now import normalize_keyword


class KeywordIdentityTests(unittest.TestCase):
    def test_unicode_entities_case_and_whitespace_share_identity(self):
        display, identity = normalize_keyword_text("Cafe\u0301   &amp;  Coffee")
        other_display, other_identity = normalize_keyword_text("ＣＡＦÉ & Coffee")

        self.assertEqual(display, "Café & Coffee")
        self.assertEqual(other_display, "CAFÉ & Coffee")
        self.assertEqual(identity, other_identity)
        self.assertEqual(canonical_keyword_digest(display), canonical_keyword_digest(other_display))

    def test_typographic_dashes_normalize_but_meaningful_punctuation_stays(self):
        self.assertEqual(canonical_keyword_digest("WH\u20111000XM5"),
                         canonical_keyword_digest("WH-1000XM5"))
        self.assertNotEqual(canonical_keyword_digest("C++"), canonical_keyword_digest("C"))
        self.assertNotEqual(canonical_keyword_digest("H&M"), canonical_keyword_digest("HM"))
        self.assertNotEqual(canonical_keyword_digest("A/B"), canonical_keyword_digest("AB"))

    def test_google_trends_normalizer_uses_shared_identity(self):
        _, first = normalize_keyword("Cafe\u0301 &amp; Coffee")
        _, second = normalize_keyword("CAFÉ & Coffee")
        self.assertEqual(first, second)

    def test_invalid_control_text_is_rejected(self):
        for text in ("", " \t ", "Coffee\x00Maker"):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                normalize_keyword_text(text)


if __name__ == "__main__":
    unittest.main()
