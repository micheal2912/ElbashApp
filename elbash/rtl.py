"""Helpers for Hebrew text that arrives in visual (reversed) order."""

import re

HEBREW = re.compile(r"[\u0590-\u05FF]")


def has_hebrew(text: str) -> bool:
    return bool(HEBREW.search(text or ""))


def fix_rtl(line: str) -> str:
    """Rebuild logical-order Hebrew from a visually-ordered PDF text line.

    pdfplumber emits RTL runs right-to-left, so both the token order and the
    characters inside each Hebrew token are reversed. Numbers and Latin tokens
    are already correct and must not be flipped.
    """
    tokens = (line or "").split()
    out = []
    for token in reversed(tokens):
        out.append(token[::-1] if HEBREW.search(token) else token)
    return " ".join(out)
