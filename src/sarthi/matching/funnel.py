"""The match funnel, cheapest layer first (docs/ARCHITECTURE.md §2.7).

L1 hard rules -> L0 free score -> L2 AI score (only for jobs still in the running) -> decision.
A job that fails a rule or scores too low on the free layers never costs an AI call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sarthi.jobs.models import JobPosting
from sarthi.matching.decide import Decision, Outcome, Thresholds, decide
from sarthi.matching.prefilter import prefilter
from sarthi.matching.rules import check_rules
from sarthi.profile.models import Profile

AI_WEIGHT = 0.6  # the AI's read of the full job counts more than keyword overlap
MAX_REASONS = 4


@dataclass(frozen=True, slots=True)
class AiScore:
    """The AI layer's verdict: 0-100 fit, how sure it is (0-1), and its reasons."""

    score: int
    confidence: float
    reasons: tuple[str, ...] = ()


class AiScorer(Protocol):
    """Layer 2. Returns None when it cannot answer (no model set up, budget spent, provider down)."""

    async def score(self, job: JobPosting, profile: Profile) -> AiScore | None:
        """Judge how well ``job`` fits ``profile``."""
        ...


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Everything the next steps need: score, decision, and the lines shown to the user."""

    score: int
    outcome: Outcome
    reasons: tuple[str, ...]
    notes: tuple[str, ...]
    ai_used: bool

    @property
    def decision(self) -> Decision:
        """Shortcut for ``outcome.decision``."""
        return self.outcome.decision


def _clamp(value: float) -> int:
    return max(0, min(100, round(value)))


async def evaluate(
    job: JobPosting,
    profile: Profile,
    now: datetime,
    thresholds: Thresholds = Thresholds(),  # noqa: B008  (frozen dataclass, safe default)
    ai: AiScorer | None = None,
) -> MatchResult:
    """Run the funnel for one job."""
    rules = check_rules(job, profile)
    if not rules.eligible:
        outcome = decide(rules, 0, thresholds)
        return MatchResult(0, outcome, (), rules.notes, ai_used=False)

    free = prefilter(job, profile, now)
    free_score = _clamp(free.score + rules.location_points)
    reasons = free.reasons
    ai_verdict: AiScore | None = None
    if ai is not None and free_score >= thresholds.hide_below:
        ai_verdict = await ai.score(job, profile)

    if ai_verdict is None:
        score, confidence = free_score, None
    else:
        score = _clamp((1 - AI_WEIGHT) * free_score + AI_WEIGHT * ai_verdict.score)
        confidence = ai_verdict.confidence
        reasons = ai_verdict.reasons + reasons

    outcome = decide(rules, score, thresholds, confidence)
    return MatchResult(score, outcome, reasons[:MAX_REASONS], rules.notes, ai_used=ai_verdict is not None)
