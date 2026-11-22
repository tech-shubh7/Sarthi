"""What a job's own text asks for, read without AI: the minimum years of experience.

The years pattern follows StillOpen's ``matchcard._YEARS`` (Apache-2.0), narrowed to phrases that mention
experience, so "10 years of company history" or "a 2 year contract" is not read as a requirement.
"""

from __future__ import annotations

import re

_DASH = r"(?:-|\u2013|\u2014|to)"
_YEARS = r"(?:years?|yrs?)\b"
_YEARS_OF_EXPERIENCE = re.compile(
    rf"\b(\d{{1,2}})\s*(?:\+|plus)?\s*(?:{_DASH}\s*\d{{1,2}}\s*)?\+?\s*{_YEARS}"
    r"(?:\s+of)?(?:\s+[\w/+#.-]+){0,5}?\s+(?:experience|exp\b|roles?\b)",
    re.IGNORECASE,
)
# The shapes real postings use besides "3 years of experience" (found on 614 real jobs, 2026-11-22):
# "Experience: 3-5 Years", "Experience Required: 4+ years", "Exp: Min 7+ Years".
_LABELLED = re.compile(
    r"\b(?:(?:total|relevant|work|overall|minimum|min\.?)\s+)?(?:experience|exp\.?)"
    r"(?:\s+(?:required|needed|level|range))?\s*[:\-\u2013\u2014]\s*"
    rf"(?:min(?:imum)?\.?\s*|at\s+least\s*)?(\d{{1,2}})\s*\+?\s*(?:{_DASH}\s*(\d{{1,2}})\s*)?\+?\s*{_YEARS}",
    re.IGNORECASE,
)
# "minimum 3 years", "at least 2 yrs", "needs 7+ years", "requires 4 years"
_MINIMUM = re.compile(
    rf"\b(?:minimum(?:\s+of)?|min\.?|at\s+least|needs?|requires?|required)\s+(\d{{1,2}})\s*\+?\s*"
    rf"(?:{_DASH}\s*\d{{1,2}}\s*)?{_YEARS}",
    re.IGNORECASE,
)
# Phrases like 3+ years in AI/ML development, 8+ years of hands-on Python, or a line 3+ years: the "+"
# marks a requirement (company age, for more than 25 years, never has one). Also 3-6 years hands-on.
_PLUS = re.compile(
    rf"\b(\d{{1,2}})\s*\+\s*{_YEARS}(?=\s+(?:in|of|with|as|for|hands[- ]on)\b|\s*$|\s*[,.;)])",
    re.IGNORECASE | re.MULTILINE,
)
_HANDS_ON = re.compile(rf"\b(\d{{1,2}})\s*(?:{_DASH}\s*\d{{1,2}}\s*)?{_YEARS}\s+hands[- ]on", re.IGNORECASE)
# "7 years of overall experience", "Overall Testing: 9+ years", "4 to 6 years of total experience": the whole
# career asked for, which one skill's "Playwright: 2+ years" cannot lower.
_OVERALL = re.compile(
    rf"\b(?:overall|total)\b[^.\n\d]{{0,30}}?(\d{{1,2}})\s*\+?\s*(?:{_DASH}\s*\d{{1,2}}\s*)?\+?\s*{_YEARS}"
    rf"|\b(\d{{1,2}})\s*\+?\s*(?:{_DASH}\s*\d{{1,2}}\s*)?\+?\s*{_YEARS}\s+(?:of\s+)?(?:overall|total)\b",
    re.IGNORECASE,
)
# A skill with its years in brackets: Node.js (4+ years), React (3+ yrs).
_IN_BRACKETS = re.compile(rf"\(\s*(\d{{1,2}})\s*\+\s*{_YEARS}\s*\)", re.IGNORECASE)
# A skill line: Back-end development: 3 years (Required), Automation Engineering: 4+ years, BDD - 4+ yrs.
_SKILL_LINE = re.compile(
    rf"^\W*([A-Za-z][\w ./+#&()-]{{1,40}}?)\s*[:\-\u2013]\s*(\d{{1,2}})\s*\+?\s*"
    rf"(?:{_DASH}\s*\d{{1,2}}\s*)?\+?\s*{_YEARS}",
    re.IGNORECASE | re.MULTILINE,
)
# A line that is only a range: "6-9 Years", "3 to 5 years"
_BARE_RANGE = re.compile(
    rf"^\W*(\d{{1,2}})\s*\+?\s*{_DASH}\s*\d{{1,2}}\s*{_YEARS}\W*$", re.IGNORECASE | re.MULTILINE
)
# Labels that give a length of time but not experience: "Contract: 2 years", "Duration - 3 years".
_NOT_EXPERIENCE = re.compile(
    r"\b(?:contract|duration|tenure|period|warranty|bond|age|term|validity|notice)\b", re.I
)
_MAX_PLAUSIBLE_YEARS = 30
_DASH_LOST_BELOW = 10  # "Experience: 37 Years": a "3-7" whose dash a feed dropped, not thirty-seven years
_DASH_LOST_SPAN = 6
# Titles often carry the range themselves: "Java Support Engineer - (8-10) YRS", "Generative AI 5- 9yrs".
_YEARS_IN_TITLE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:\+|plus)?\s*\)?\s*(?:(?:-|\u2013|to)\s*\(?\d{1,2}\s*\)?\s*)?(?:years?|yrs?)(?![a-z])",
    re.IGNORECASE,
)


def min_years_in_title(title: str) -> int | None:
    """The years a job title itself asks for ("Data Scientist 5 To 12 Years" -> 5), or None."""
    years = [int(m.group(1)) for m in _YEARS_IN_TITLE.finditer(title or "")]
    plausible = [n for n in years if 0 < n <= _MAX_PLAUSIBLE_YEARS]
    return min(plausible) if plausible else None


def min_years_required(text: str) -> int | None:
    """The smallest "N years of experience" the text asks for, or None when it states none.

    The smallest is used so a nice-to-have "10+ years of leadership experience" never hides a job whose
    main requirement is "3+ years of Python experience". Besides "N years of experience" it reads the
    labelled shapes postings use ("Experience: 3-5 Years", "Node.js (4+ years)", a line "6-9 Years").
    """
    text = (text or "").replace("**", "").replace("__", "").replace("\\", "")
    years = [int(m.group(1)) for m in _YEARS_OF_EXPERIENCE.finditer(text)]
    for m in _LABELLED.finditer(text):
        years.append(_dash_lost(int(m.group(1))) if m.group(2) is None else int(m.group(1)))
    years += [int(m.group(1)) for m in _MINIMUM.finditer(text)]
    years += [int(m.group(1)) for m in _IN_BRACKETS.finditer(text)]
    years += [int(m.group(1)) for m in _PLUS.finditer(text)]
    years += [int(m.group(1)) for m in _HANDS_ON.finditer(text)]
    years += [int(m.group(2)) for m in _SKILL_LINE.finditer(text) if not _NOT_EXPERIENCE.search(m.group(1))]
    years += [int(m.group(1)) for m in _BARE_RANGE.finditer(text)]
    plausible = [n for n in years if 0 < n <= _MAX_PLAUSIBLE_YEARS]
    if not plausible:
        return None
    overall = [int(m.group(1) or m.group(2)) for m in _OVERALL.finditer(text)]
    overall = [n for n in overall if 0 < n <= _MAX_PLAUSIBLE_YEARS]
    return max(min(plausible), min(overall)) if overall else min(plausible)


def _dash_lost(n: int) -> int:
    """37 -> 3 when the number reads as a range whose dash was dropped ("3-7"); else n unchanged."""
    first, second = divmod(n, 10)
    if n > _DASH_LOST_BELOW and 0 < first < second and second - first <= _DASH_LOST_SPAN:
        return first
    return n


# A job asking a little more than the user has is still worth showing: 1 year early in a career (1 year of
# experience vs "3+ years" is not a fit), 2 years later on.
EARLY_CAREER_YEARS = 3
# Typical minimum years for a level stated in a title ("SDE II", "Senior", "SDE III", "Staff", "Engineering
# Manager"). "II" asks for 2-3 years, so with the early-career slack it shows from 2 years. "Manager" is gated
# only for people who do not target manager roles themselves ("Product Manager" is a role, not a level).
LEVEL_MIN_YEARS: dict[str, int] = {"mid": 3, "senior": 4, "lead": 6, "manager": 6, "principal": 8}


def years_tolerance(years: float) -> int:
    """How many years above the user's experience a job may still ask for."""
    return 1 if years < EARLY_CAREER_YEARS else 2


def too_experienced_for(needed: float, years: float) -> bool:
    """True when a job asking for ``needed`` years is out of reach with ``years`` of experience."""
    return needed > years + years_tolerance(years)
