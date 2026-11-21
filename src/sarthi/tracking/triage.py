# Ported from ApplyPilot (AGPL-3.0), src/applypilot/tracking/triage.py (ibarrajo fork).
# Modifications (c) Sarthi contributors: one Triage result per email; Indian job boards added to the alerts.
"""Sorting job emails with rules first, so most never need an AI call.

Order (first match wins): interviews, offers, assessments and "next steps" always go to the AI (it extracts
dates and people); job alerts and newsletters are noise; "thank you for applying" is a confirmation; "we will
not be moving forward" is a rejection; anything else goes to the AI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    """What a job email is."""

    CONFIRMATION = "confirmation"
    REJECTION = "rejection"
    INTERVIEW = "interview"
    ASSESSMENT = "assessment"  # a test, take-home or more information asked for
    OFFER = "offer"
    OTHER = "other"  # about the application, none of the above
    NOISE = "noise"


@dataclass(frozen=True, slots=True)
class Triage:
    """A rule's verdict, or ``needs_ai`` when only a model can tell."""

    kind: Kind | None
    needs_ai: bool
    reason: str


def _all(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.I) for p in patterns)


_AI_SUBJECT = _all(
    r"\binterview\b", r"\bschedul(?:e|ed|ing)\b", r"\bnext\s+steps?\b", r"\boffer\b", r"\bassessment\b",
    r"\bcoding\s+(?:challenge|test|round)\b", r"\btechnical\s+(?:screen|round)\b", r"\bphone\s+screen\b",
    r"\bbackground\s+(?:check|verification)\b", r"\bonboarding\b", r"\bcompensation\b", r"\bsalary\b", r"\bctc\b",
)  # fmt: skip
_AI_BODY = _all(
    r"\bschedule\s+(?:a|an|your)\s+(?:interview|call)\b", r"\binvit(?:e|ed|ing)\s+you\s+to\b", r"\bnext\s+steps?\b",
    r"\boffer\s+letter\b", r"\bcoding\s+(?:challenge|assessment|test)\b", r"\btake[\s-]home\b", r"\bhackerrank\b",
    r"\bcodility\b", r"\bcalendly\.com\b",
)  # fmt: skip
NOISE_DOMAINS = frozenset(
    {
        "linkedin.com", "linotify.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "dice.com", "monster.com",
        "naukri.com", "foundit.in", "instahyre.com", "substack.com", "medium.com", "careerbuilder.com",
    }
)  # fmt: skip
_NOISE_SUBJECT = _all(
    r"\bjob\s+alert", r"\bnew\s+jobs?\s+(?:for|matching|near)\b", r"\bnewsletter\b", r"\bdigest\b",
    r"\bweekly\s+(?:update|recap|summary)\b", r"\brecommended\s+jobs?\b", r"\bjobs?\s+you\s+might\s+like\b",
    r"\bsimilar\s+jobs?\b", r"\bprofile\s+view", r"\bwho\s+viewed\s+your\b", r"\bconnection\s+request\b",
    r"\bendorsed\b", r"\bjobs?\s+matching\s+your\b",
)  # fmt: skip
_CONFIRM = _all(
    r"thank\s+you\s+for\s+(?:your\s+)?appl(?:ying|ication)", r"application\s+(?:received|submitted|confirmed)",
    r"we\s+(?:have\s+)?received\s+your\s+application", r"successfully\s+(?:submitted|applied)",
    r"your\s+application\s+(?:has\s+been\s+)?(?:received|submitted)", r"thanks?\s+for\s+(?:your\s+)?interest",
    r"we(?:'ve|'ll|\s+will)\s+review\s+your\s+(?:application|resume|profile)",
)  # fmt: skip
_REJECT = _all(
    r"\bunfortunately\b", r"\bnot\s+(?:be\s+)?moving\s+forward\b", r"\bposition\s+(?:has\s+been\s+)?filled\b",
    r"\bdecided\s+(?:not\s+)?to\s+(?:move|proceed)\s+(?:forward\s+)?with\s+other\b",
    r"\bwill\s+not\s+be\s+(?:moving|proceeding)\b", r"\bpursuing\s+other\s+candidates\b",
    r"\bregret\s+to\s+inform\b", r"\bunable\s+to\s+offer\b",
    r"\bwe\s+(?:have\s+)?decided\s+to\s+go\s+(?:with|in)\s+(?:a\s+)?(?:another|different)\b",
)  # fmt: skip


def _domain_root(sender: str) -> str:
    domain = sender.rsplit("@", 1)[-1].strip().lower().rstrip(">")
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain  # noqa: PLR2004


def triage(sender: str, subject: str, body: str) -> Triage:
    """The rule verdict for one email (``body``: plain text, first few thousand characters are enough)."""
    head = body[:3000]
    if any(p.search(subject) for p in _AI_SUBJECT) or any(p.search(head) for p in _AI_BODY):
        return Triage(None, needs_ai=True, reason="interview, offer or next steps")
    if _domain_root(sender) in NOISE_DOMAINS or any(p.search(subject) for p in _NOISE_SUBJECT):
        return Triage(Kind.NOISE, needs_ai=False, reason="job alert or newsletter")
    if any(p.search(f"{subject}\n{head}") for p in _REJECT):
        return Triage(Kind.REJECTION, needs_ai=False, reason="rejection wording")
    if any(p.search(f"{subject}\n{head}") for p in _CONFIRM):
        return Triage(Kind.CONFIRMATION, needs_ai=False, reason="confirmation wording")
    return Triage(None, needs_ai=True, reason="unclear")
