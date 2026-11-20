# Ported from OpenApply (MIT), src/match.ts (scoreJob) @ db7796e. Copyright (c) OpenApply contributors.
# Modifications (c) Sarthi contributors.
# Changes: TypeScript -> Python; location scoring moved to sarthi.matching.rules (ISO countries instead
# of a US-centred place regex); the salary floor is compared only within the same currency and period;
# resume bullet terms come from plain resume text; seniority uses Sarthi's levels; tech names such as
# node.js are no longer stemmed to node.j; with no seniority set, levels are inferred from years of
# experience.
"""Layer 0 of the match funnel: a free, instant score from title, skills, seniority, salary and recency."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from sarthi.jobs.models import JobPosting, Seniority
from sarthi.profile.models import Profile

_STOP_WORDS = """
a an and are as at be by for from has have in is it of on or our that the their this to we with you
your will can all any not but if more most other such than then them they who what when where which
while about across into over per via experience work working team teams role company years year
strong ability skills including etc using use new help build building looking join us job position
candidate responsibilities requirements qualifications benefits equal opportunity employer
"""
_STOP = frozenset(_STOP_WORDS.split())
_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]")
_MIN_STEM_LENGTH = 3
_MIN_BULLET_TERM_LENGTH = 3
_MAX_KEYWORD_LENGTH = 40
_MAX_BULLET_TERMS = 40

TITLE_POINTS = 40
KEYWORD_POINTS = 35
SENIORITY_FIT_POINTS = 5
SENIORITY_STEP_PENALTY = 7
SENIORITY_MAX_PENALTY = 15
SALARY_POINTS = 5
SALARY_PENALTY = 20
_PARTIAL_ORDER_CREDIT = 0.75
_TITLE_IN_KEYWORD_BONUS = 1.6
_BULLET_TERM_WEIGHT = 0.35
_MIN_KEYWORD_DENOMINATOR = 4
_MAX_KEYWORD_DENOMINATOR = 9
_SHOWN_KEYWORDS = 6
_MIN_BULLET_HITS_TO_MENTION = 3
_RECENCY_POINTS: tuple[tuple[int, int], ...] = ((3, 10), (7, 8), (14, 6), (30, 4), (60, 2))
_MENTION_RECENT_DAYS = 7

_LEVEL: dict[Seniority, int] = {
    Seniority.JUNIOR: 1,
    Seniority.MID: 2,
    Seniority.SENIOR: 3,
    Seniority.LEAD: 4,
    Seniority.PRINCIPAL: 4,
    Seniority.MANAGER: 4,
}
_DEFAULT_LEVEL = 2  # an unmarked title counts as mid-level
# Levels a person with N years of experience fits when they set none: (up to years, levels).
_LEVELS_BY_YEARS: tuple[tuple[float, frozenset[int]], ...] = (
    (1, frozenset({1})),
    (3, frozenset({1, 2})),
    (6, frozenset({2, 3})),  # "Senior" often means 3-5 years, e.g. in India
    (10, frozenset({3, 4})),
)
_SENIOR_LEVELS = frozenset({4})


def _levels_for_years(years: float | None) -> frozenset[int]:
    """Seniority levels implied by years of experience; empty when unknown."""
    if years is None:
        return frozenset()
    return next((levels for limit, levels in _LEVELS_BY_YEARS if years < limit), _SENIOR_LEVELS)


def _stem(word: str) -> str:
    """Plural to singular ("apis" -> "api"); tech names with ``.``, ``+`` or ``#`` ("node.js") are kept."""
    if len(word) <= _MIN_STEM_LENGTH or not word.endswith("s") or word.endswith("ss") or not word.isalnum():
        return word
    return word[:-1]


def tokens(text: str) -> list[str]:
    """Lower-cased word tokens, keeping tech names such as ``c++``, ``c#`` and ``node.js``."""
    return [_stem(t) for t in _TOKEN.findall(text.lower())]


def _significant(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in _STOP and len(t) > 1]


@dataclass(frozen=True, slots=True)
class _Context:
    titles: tuple[tuple[str, ...], ...]
    keywords: tuple[tuple[str, tuple[str, ...]], ...]  # (phrase, tokens)
    bullet_terms: tuple[str, ...]
    levels: frozenset[int]


@lru_cache(maxsize=8)
def _context(profile: Profile) -> _Context:
    """Everything about the user that scoring needs, built once per profile (profiles are immutable)."""
    prefs = profile.preferences
    titles = tuple(tuple(t) for t in map(_significant, prefs.roles) if t)
    phrases: dict[str, None] = {}
    for raw in (*prefs.keywords, *profile.skills):
        phrase = re.sub(r"\s*\(.*\)\s*", " ", raw.lower()).strip()
        if phrase and len(phrase) < _MAX_KEYWORD_LENGTH:
            phrases[phrase] = None
    keywords = tuple((p, tuple(tokens(p))) for p in phrases if tokens(p))
    known = {t for _, toks in keywords for t in toks}
    counts = Counter(
        w
        for w in _significant(profile.resume_text)
        if not w[0].isdigit() and len(w) > _MIN_BULLET_TERM_LENGTH and w not in known
    )
    bullet_terms = tuple(w for w, _ in counts.most_common(_MAX_BULLET_TERMS))
    levels = frozenset(_LEVEL[s] for s in prefs.seniority if s in _LEVEL) or _levels_for_years(
        prefs.years_experience
    )
    return _Context(titles, keywords, bullet_terms, levels)


@dataclass(frozen=True, slots=True)
class PrefilterResult:
    """Score out of 85 (location's 15 points are added by the rules layer) and why."""

    score: float
    reasons: tuple[str, ...]


def _title_score(job: JobPosting, ctx: _Context) -> tuple[float, str | None]:
    job_words = set(_significant(job.title))
    sequence = f" {' '.join(_significant(job.title))} "
    best = 0.0
    best_title: tuple[str, ...] = ()
    for title in ctx.titles:
        share = sum(1 for w in title if w in job_words) / len(title)
        # All words but not in order ("Product Security Engineer" for "product engineer"): partial credit.
        if share == 1 and len(title) > 1 and f" {' '.join(title)} " not in sequence:
            share = _PARTIAL_ORDER_CREDIT
        if share > best:
            best, best_title = share, title
    if best == 0:
        return 0.0, "Title does not match your target roles" if ctx.titles else None
    points = TITLE_POINTS * (1 if best == 1 else best * best * 0.8)
    if best == 1:
        return points, f"Title matches your target role ({' '.join(best_title)})"
    return points, f'Title partly matches ({round(best * 100)}% of "{" ".join(best_title)}")'


def _keyword_score(job: JobPosting, ctx: _Context) -> tuple[float, str | None]:
    if not ctx.keywords and not ctx.bullet_terms:
        return 0.0, None
    text = f"{job.title} {job.description}".lower()
    body = set(tokens(text))
    title_tokens = set(tokens(job.title))
    matched = 0.0
    hits: list[str] = []
    for phrase, toks in ctx.keywords:
        found = toks[0] in body if len(toks) == 1 else phrase in text
        if found:
            matched += _TITLE_IN_KEYWORD_BONUS if all(t in title_tokens for t in toks) else 1
            hits.append(phrase)
    bullet_hits = sum(1 for w in ctx.bullet_terms if w in body)
    matched += bullet_hits * _BULLET_TERM_WEIGHT
    denominator = min(max(len(ctx.keywords), _MIN_KEYWORD_DENOMINATOR), _MAX_KEYWORD_DENOMINATOR)
    points = KEYWORD_POINTS * min(1.0, matched / denominator)
    if hits:
        more = len(hits) - _SHOWN_KEYWORDS
        return points, f"Mentions {', '.join(hits[:_SHOWN_KEYWORDS])}" + (
            f" and {more} more" if more > 0 else ""
        )
    if bullet_hits >= _MIN_BULLET_HITS_TO_MENTION:
        return points, f"Shares {bullet_hits} terms with your resume"
    return points, None


def _seniority_score(job: JobPosting, ctx: _Context) -> tuple[float, str | None]:
    if not ctx.levels:
        return 0.0, None
    known = job.seniority in _LEVEL
    level = _LEVEL.get(job.seniority, _DEFAULT_LEVEL)
    distance = min(abs(wanted - level) for wanted in ctx.levels)
    if distance == 0:
        return SENIORITY_FIT_POINTS, "Seniority fits" if known else None
    return -min(SENIORITY_MAX_PENALTY, distance * SENIORITY_STEP_PENALTY), f"Seniority mismatch ({job.title})"


def _salary_score(job: JobPosting, profile: Profile) -> tuple[float, str | None]:
    floor, offer = profile.preferences.min_salary, job.salary
    if floor is None or offer is None or floor.minimum is None:
        return 0.0, None
    if (offer.currency, offer.period) != (floor.currency, floor.period):
        return 0.0, None  # different currency or period: no fair comparison without conversion
    top = offer.maximum if offer.maximum is not None else offer.minimum
    if top is None:
        return 0.0, None
    if top < floor.minimum:
        return (
            -SALARY_PENALTY,
            f"Pays up to {offer.currency} {top:,.0f}, below your {floor.minimum:,.0f} floor",
        )
    return SALARY_POINTS, f"Pays up to {offer.currency} {top:,.0f}"


def _recency_score(job: JobPosting, now: datetime) -> tuple[float, str | None]:
    posted = job.posted_at or job.fetched_at
    days = max(0.0, (now - posted).total_seconds() / 86400)
    points = next((p for limit, p in _RECENCY_POINTS if days <= limit), 0)
    if days <= _MENTION_RECENT_DAYS:
        whole = round(days)
        return points, f"Posted {whole} day{'' if whole == 1 else 's'} ago"
    return points, None


def prefilter(job: JobPosting, profile: Profile, now: datetime) -> PrefilterResult:
    """Score ``job`` against ``profile`` without any AI call."""
    ctx = _context(profile)
    parts = (
        _title_score(job, ctx),
        _keyword_score(job, ctx),
        _seniority_score(job, ctx),
        _salary_score(job, profile),
        _recency_score(job, now),
    )
    score = sum(points for points, _ in parts)
    reasons = tuple(reason for _, reason in parts if reason)
    return PrefilterResult(score=score, reasons=reasons)
