"""Removing personal contact details from text before it is sent to an AI provider.

The resume is needed for matching, but the AI does not need the user's email, phone number or profile links.
Names cannot be removed reliably and are left as they are; documented in docs/STANDARDS.md §4.
"""

from __future__ import annotations

import re

_REMOVED = "[removed]"
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # email
    re.compile(r"(?i)\b(?:https?://|www\.)\S+"),  # links (LinkedIn, GitHub, portfolio)
    re.compile(r"(?i)\b(?:linkedin|github)\.com/\S+"),
    re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,}\d(?![\w.])"),  # phone numbers (9+ digits with separators)
)


def scrub(text: str) -> str:
    """``text`` with emails, phone numbers and links replaced by ``[removed]``."""
    for pattern in _PATTERNS:
        text = pattern.sub(_REMOVED, text)
    return text
