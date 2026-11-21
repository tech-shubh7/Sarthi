# Ported from ApplyPilot (AGPL-3.0), src/applypilot/tracking/matcher.py (ibarrajo fork).
# Modifications (c) Sarthi contributors: matches Sarthi's applications; the job board a job was found on is
# never taken for the employer.
"""Which application an email is about, from several signals added up (none is enough alone).

Signals: the sender's domain is the employer's (40); the employer's name is in the subject (25) or body (15); the
company named in a subject like "Thank you for applying to Acme" is the employer (35); words of the job title in
the subject (20, or 10 for one); a hiring-system sender such as Greenhouse (10); sent within 30 days of applying
(5). The best application with 40 or more points wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

ATS_DOMAINS = frozenset(
    {
        "greenhouse.io", "greenhouse-mail.io", "lever.co", "icims.com", "myworkdayjobs.com", "workday.com",
        "jobvite.com", "smartrecruiters.com", "workable.com", "ashbyhq.com", "breezy.hr", "recruitee.com",
        "darwinbox.in", "keka.com", "zohorecruit.com", "freshteam.com",
    }
)  # fmt: skip
ATS_PREFIXES = frozenset(
    {"noreply", "no-reply", "notifications", "careers", "jobs", "talent", "recruiting", "hr"}
)
BOARD_DOMAINS = frozenset(
    {"linkedin.com", "indeed.com", "naukri.com", "glassdoor.com", "foundit.in", "instahyre.com"}
)  # a job found on a board: its domain says nothing about the employer
MIN_SCORE = 40
NEAR_DAYS = 30
_MIN_NAME = 2
_MAX_NAME_WORDS = 5

_SUBJECT_COMPANY = tuple(
    re.compile(p, re.I)
    for p in (
        r"(?:applying|applied)\s+(?:to|at)\s+(.+?)(?:\s*[!.,]|\s+\b(?:has|have|had|was|is|are|will|would)\b|$)",
        r"(?:position|role|job|opportunity)\s+at\s+(.+?)(?:\s*[!.,]|\s+\b(?:has|have|had|was|is|are|will|would)\b|$)",
        r"(?:application|interest)\s+(?:to|at|in|for)\s+(.+?)(?:\s*[!.,]|\s+\b(?:has|have|had|was|is|are|will|would)\b|$)",
        r"thank\s+you\s+from\s+(.+?)(?:\s*[!.,]|$)",
        r"^(.+?)\s*[:|]\s+(?:thank\s+you|we\s+received|application|your\s+application)",
    )
)
_GENERIC = frozenset(
    {"us", "the", "you", "your", "we", "our", "it", "a", "an", "this", "that", "they", "their", "its"}
)
_TITLE_WORDS = frozenset(
    {"engineer", "developer", "manager", "director", "analyst", "scientist", "architect", "designer", "consultant",
     "specialist", "senior", "junior", "staff", "principal", "associate", "lead", "head", "intern"}
)  # fmt: skip
_LEGAL = re.compile(
    r"\s*,?\s+(?:inc|llc|ltd|limited|corp|co|plc|gmbh|ag|sa|bv|nv|pty|pte|pvt|private|srl|sas)\.?\s*$", re.I
)
_STOP = frozenset({"the", "a", "an", "and", "or", "at", "in", "for", "of", "to", "with", "is", "are", "we"})


@dataclass(frozen=True, slots=True)
class Applied:
    """One application, as the matcher needs it."""

    job_id: str
    company: str
    title: str
    form_url: str
    applied_at: datetime | None


@dataclass(frozen=True, slots=True)
class Match:
    """The application an email is about, its score and why."""

    job_id: str
    score: int
    signals: tuple[str, ...]


def company_from_subject(subject: str) -> str | None:
    """The company a subject names ("Thank you for applying to Acme" -> "Acme"), or None."""
    for pattern in _SUBJECT_COMPANY:
        m = pattern.search(subject.strip())
        if not m:
            continue
        candidate = m.group(1).strip().strip("!.,")
        words = set(candidate.lower().split())
        if (
            len(candidate) < _MIN_NAME
            or candidate.lower() in _GENERIC
            or len(candidate.split()) > _MAX_NAME_WORDS
            or words & _TITLE_WORDS
        ):
            continue
        return candidate
    return None


def normalise_company(name: str) -> str:
    """Lower case, legal suffixes and punctuation dropped ("Acme Pvt. Ltd." -> "acme")."""
    previous = None
    while previous != name:
        previous, name = name, _LEGAL.sub("", name)
    return re.sub(r"[^\w\s]", "", name).lower().strip()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _root(domain: str) -> str:
    parts = domain.lower().strip(">").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain.lower()  # noqa: PLR2004


def _host(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def _score(  # noqa: PLR0913  (the email's parts)
    app: Applied, sender: str, subject: str, body: str, *, sent: datetime | None, named: str
) -> Match:
    score, signals = 0, []
    sender_domain = sender.rsplit("@", 1)[-1].lower().strip(">") if "@" in sender else ""
    sender_root, sender_local = (
        _root(sender_domain),
        sender.split("@", maxsplit=1)[0].lower().strip("<") if "@" in sender else "",
    )
    company = normalise_company(app.company)
    form_root = _root(_host(app.form_url))
    low_subject, low_body = subject.lower(), body[:2000].lower()
    if form_root and form_root not in BOARD_DOMAINS | ATS_DOMAINS and sender_root == form_root:
        score += 40
        signals.append(f"domain:{sender_root}")
    elif len(company) > 3 and sender_root and _slug(company) == _slug(sender_root.split(".")[0]):  # noqa: PLR2004
        score += 40
        signals.append(f"company_domain:{sender_root}")
    if len(company) > 2 and (company in low_subject or _slug(company) in _slug(low_subject)):  # noqa: PLR2004
        score += 25
        signals.append("company_in_subject")
    elif len(company) > 2 and company in low_body:  # noqa: PLR2004
        score += 15
        signals.append("company_in_body")
    if named and company:
        a, b = _slug(normalise_company(named)), _slug(company)
        if a and b and (a == b or a in b or b in a):
            score += 35
            signals.append(f"named:{named}")
    title_words = {w for w in re.findall(r"[a-z]+", app.title.lower()) if len(w) > 2 and w not in _STOP}  # noqa: PLR2004
    overlap = title_words & set(re.findall(r"[a-z]+", low_subject))
    if len(overlap) >= 2:  # noqa: PLR2004
        score += 20
        signals.append("title")
    elif len(overlap) == 1:
        score += 10
        signals.append("title_word")
    if any(sender_domain.endswith(d) for d in ATS_DOMAINS) or sender_local in ATS_PREFIXES:
        score += 10
        signals.append("ats_sender")
    if sent and app.applied_at and abs((sent - app.applied_at).days) <= NEAR_DAYS:
        score += 5
        signals.append("recent")
    return Match(app.job_id, score, tuple(signals))


def match(
    sender: str, subject: str, body: str, sent: datetime | None, applications: list[Applied]
) -> Match | None:
    """The application the email is most likely about (score 40+), or None."""
    named = company_from_subject(subject) or ""
    best: Match | None = None
    for app in applications:
        found = _score(app, sender, subject, body, sent=sent, named=named)
        if found.score >= MIN_SCORE and (best is None or found.score > best.score):
            best = found
    return best
