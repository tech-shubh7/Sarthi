"""Learning from the user's taps: the instant-alert line moves with their Apply / Skip choices.

JobRadar suggests a threshold change from feedback; Sarthi applies it automatically, in small steps and within
fixed bounds, so a few taps can never silence alerts or flood them:

- nothing changes before ``MIN_CHOICES`` taps;
- of the last ``RECENT`` taps, mostly Skip moves the alert line up (stricter) by up to ``MAX_SHIFT`` points,
  mostly Apply moves it down by up to ``MAX_EASE``: too many alerts cost more than a borderline job that still
  reaches the daily digest; never outside ``ALERT_BOUNDS``;
- the AI is still asked about every job close enough to the line (``ai_from`` follows it).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from sarthi.matching.decide import Thresholds

RECENT = 20
MIN_CHOICES = 8
MAX_SHIFT = 10
MAX_EASE = 5
ALERT_BOUNDS = (55, 80)
AI_BELOW_ALERT = 10  # the AI reads jobs from this many points under the alert line
AI_FROM_FLOOR = 40


@dataclass(frozen=True, slots=True)
class Learned:
    """The thresholds in use and how they were reached (for /status)."""

    thresholds: Thresholds
    shift: int
    choices: int


def learned_thresholds(base: Thresholds, choices: list[tuple[bool, int]]) -> Learned:
    """``base`` moved by the user's recent ``(applied, score)`` choices (newest first)."""
    recent = choices[:RECENT]
    if len(recent) < MIN_CHOICES:
        return Learned(base, 0, len(recent))
    skip_share = sum(1 for applied, _ in recent if not applied) / len(recent)
    lean = (skip_share - 0.5) * 2  # -1 (all Apply) .. +1 (all Skip)
    shift = round(lean * (MAX_SHIFT if lean > 0 else MAX_EASE))
    low, high = ALERT_BOUNDS
    alert_at = max(low, min(high, base.alert_at + shift))
    ai_from = max(AI_FROM_FLOOR, min(base.ai_from + shift, alert_at - AI_BELOW_ALERT))
    hide_below = min(base.hide_below, ai_from)
    thresholds = replace(base, alert_at=alert_at, ai_from=ai_from, hide_below=hide_below)
    return Learned(thresholds, alert_at - base.alert_at, len(recent))
