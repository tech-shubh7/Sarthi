# Decision table adapted from JobRadar (MIT), src/jobradar/pipeline/decide.py @ fdd739b.
# Copyright (c) ThePlator. Modifications (c) Sarthi contributors.
# Changes: outcomes are hide / digest / instant alert (no Notion inbox); medium scam risk can never alert;
# rules come from sarthi.matching.rules.
"""What happens to a matched job. Rules run in order; the first that applies wins.

| # | When                                         | Then          |
|---|----------------------------------------------|---------------|
| 1 | a hard rule failed (country, visa, scam, ...) | hidden        |
| 2 | score below ``hide_below``                    | hidden (weak) |
| 3 | possible scam, or the AI was unsure           | daily digest  |
| 4 | score at or above ``alert_at``                | instant alert |
| 5 | otherwise                                     | daily digest  |
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sarthi.matching.rules import RulesResult


class Decision(StrEnum):
    """Where a job goes."""

    HIDE = "hide"
    DIGEST = "digest"
    ALERT = "alert"


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Score cut-offs (0-100)."""

    hide_below: int = 35
    alert_at: int = 60
    min_ai_confidence: float = 0.6


@dataclass(frozen=True, slots=True)
class Outcome:
    """The decision, the table row that made it, and why a hidden job was hidden."""

    decision: Decision
    rule: str
    hidden_because: str | None = None


def decide(
    rules: RulesResult, score: int, thresholds: Thresholds, ai_confidence: float | None = None
) -> Outcome:
    """Apply the table above."""
    if not rules.eligible:
        return Outcome(Decision.HIDE, "not_eligible", rules.blockers[0] if rules.blockers else "not eligible")
    if score < thresholds.hide_below:
        return Outcome(Decision.HIDE, "weak", f"weak match ({score})")
    if rules.scam_risk != "low":
        return Outcome(Decision.DIGEST, "possible_scam")
    if ai_confidence is not None and ai_confidence < thresholds.min_ai_confidence:
        return Outcome(Decision.DIGEST, "unsure")
    if score >= thresholds.alert_at:
        return Outcome(Decision.ALERT, "strong")
    return Outcome(Decision.DIGEST, "fair")
