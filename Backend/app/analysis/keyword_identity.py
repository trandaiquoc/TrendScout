"""Conservative text normalization shared by source adapters and keyword views."""

from __future__ import annotations

import hashlib
import html
import unicodedata

_PUNCTUATION_EQUIVALENTS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u02bb": "'", "\u02bc": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-",
})


def normalize_keyword_text(value: str) -> tuple[str, str]:
    """Return a readable display form and a conservative canonical identity.

    Compatibility Unicode and HTML entities are normalized, while punctuation
    remains significant except for typographic quote and dash equivalents.
    """
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("Keyword text must be a non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("Keyword text must be valid UTF-8") from None
    decoded = html.unescape(value)
    if any(unicodedata.category(character) == "Cc"
           and character not in "\t\n\r" for character in decoded):
        raise ValueError("Keyword text is empty or contains control characters")
    display = " ".join(unicodedata.normalize("NFKC", decoded).split()).strip()
    if not display:
        raise ValueError("Keyword text is empty or contains control characters")
    identity = " ".join(
        unicodedata.normalize("NFKC", display.casefold())
        .translate(_PUNCTUATION_EQUIVALENTS).split()
    )
    if not identity:
        raise ValueError("Keyword identity is empty")
    return display, identity


def canonical_keyword_identity(value: str) -> str:
    """Return normalized identity text without discarding meaningful punctuation."""
    return normalize_keyword_text(value)[1]


def canonical_keyword_digest(value: str) -> str:
    """Return the stable SHA-256 identity used in keyword/evidence contracts."""
    identity = canonical_keyword_identity(value)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
