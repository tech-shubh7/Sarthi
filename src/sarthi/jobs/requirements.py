"""What a job's own text asks for, read without AI: the minimum years of experience.

The years pattern follows StillOpen's ``matchcard._YEARS`` (Apache-2.0), narrowed to phrases that mention
experience, so "10 years of company history" or "a 2 year contract" is not read as a requirement.
"""

from __future__ import annotations

import re

_YEARS_OF_EXPERIENCE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|\u2013|to)\s*\d{1,2}\s*)?(?:years?|yrs?)\b"
    r"(?:\s+of)?(?:\s+[\w/+#.-]+){0,4}?\s+(?:experience|exp\b)",
    re.IGNORECASE,
)
_MAX_PLAUSIBLE_YEARS = 30


def min_years_required(text: str) -> int | None:
    """The smallest "N years of experience" the text asks for, or None when it states none.

    The smallest is used so a nice-to-have "10+ years of leadership experience" never hides a job whose
    main requirement is "3+ years of Python experience".
    """
    years = [int(m.group(1)) for m in _YEARS_OF_EXPERIENCE.finditer(text or "")]
    plausible = [n for n in years if 0 < n <= _MAX_PLAUSIBLE_YEARS]
    return min(plausible) if plausible else None
