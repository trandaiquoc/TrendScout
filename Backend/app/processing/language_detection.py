"""Conservative offline lexical hints, not a calibrated language model."""
from dataclasses import dataclass
import re
import unicodedata

VERSION = 'lexical-hints-v0.1'
_EN = frozenset({'the','and','of','for','with','in','from','how','this','is'})
_VI = frozenset({'và','của','những','các','trong','cho','với','là','một','như'})


@dataclass(frozen=True)
class LanguageResult:
    language: str
    confidence: None
    status: str
    method: str
    version: str
    reason: str
    original_text: str


def detect_language(text: str) -> LanguageResult:
    if not isinstance(text, str):
        raise ValueError('title must be a string')
    tokens = re.findall(r'[^\W\d_]+', unicodedata.normalize('NFC', text).casefold(), flags=re.UNICODE)
    en, vi = set(tokens) & _EN, set(tokens) & _VI
    lang, status, reason = 'unknown', 'INSUFFICIENT_DATA', 'insufficient_unambiguous_lexical_evidence'
    if len(tokens) >= 4 and len(text) >= 16:
        if len(en) >= 2 and not vi:
            lang, status, reason = 'en', 'DETECTED', 'multiple_english_function_words'
        elif len(vi) >= 2 and not en:
            lang, status, reason = 'vi', 'DETECTED', 'multiple_vietnamese_function_words'
        elif en and vi:
            reason = 'mixed_or_ambiguous_language'
    return LanguageResult(lang, None, status, 'lexical_hints', VERSION, reason, text)
