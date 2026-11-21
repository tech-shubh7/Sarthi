import sqlite3

import pytest

from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchRecord, MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.matching.decide import Thresholds
from sarthi.matching.handler import Matcher
from sarthi.matching.learning import (
    ALERT_BOUNDS,
    MAX_EASE,
    MAX_SHIFT,
    MIN_CHOICES,
    RECENT,
    learned_thresholds,
)
from sarthi.queue.tasks import TaskQueue
from tests.conftest import FakeClock
from tests.matching.conftest import make_job, make_profile

BASE = Thresholds()


def taps(applied: int, skipped: int) -> list[tuple[bool, int]]:
    return [(True, 70)] * applied + [(False, 70)] * skipped


def test_nothing_changes_before_enough_taps() -> None:
    learned = learned_thresholds(BASE, taps(0, MIN_CHOICES - 1))
    assert (learned.thresholds, learned.shift, learned.choices) == (BASE, 0, MIN_CHOICES - 1)


@pytest.mark.parametrize(
    ("applied", "skipped", "shift"),
    [
        (0, 20, MAX_SHIFT),  # skips everything: alerts get stricter
        (20, 0, -MAX_EASE),  # applies to everything: more alerts, but eased more slowly
        (10, 10, 0),  # half and half: unchanged
        (5, 15, 5),
        (16, 4, -3),
    ],
)
def test_the_alert_line_follows_the_taps(applied: int, skipped: int, shift: int) -> None:
    learned = learned_thresholds(BASE, taps(applied, skipped))
    assert learned.shift == shift
    assert learned.thresholds.alert_at == BASE.alert_at + shift
    assert learned.thresholds.ai_from <= learned.thresholds.alert_at - 10  # the AI still reads near-misses
    assert learned.thresholds.hide_below <= learned.thresholds.ai_from


def test_only_the_recent_taps_count() -> None:
    newest_first = taps(RECENT, 0) + taps(0, 100)  # old skips no longer matter
    assert learned_thresholds(BASE, newest_first).shift == -MAX_EASE


def test_bounds_hold_whatever_the_base() -> None:
    low, high = ALERT_BOUNDS
    assert learned_thresholds(Thresholds(alert_at=78), taps(0, 20)).thresholds.alert_at == high
    assert learned_thresholds(Thresholds(alert_at=57), taps(20, 0)).thresholds.alert_at == low


def test_recent_choices_come_from_answered_shown_jobs(
    conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    matches, chats = MatchStore(conn, clock=clock), ChatStore(conn, clock=clock)
    shown = [
        (JobStatus.APPLY, "alert"),
        (JobStatus.SKIPPED, "digest"),
        (JobStatus.LATER, "alert"),  # not a choice
        (JobStatus.ALERTED, "alert"),  # not answered
        (JobStatus.APPLY, "hide"),  # hidden by today's rules: says nothing about what still qualifies
        (JobStatus.APPLIED, "alert"),
    ]
    for n, (status, decision) in enumerate(shown):
        job = make_job(
            url=f"https://boards.greenhouse.io/a/jobs/{n}", title=f"Role number {n}"
        )  # not reposts
        job_id = job_store.save(job).job_id
        matches.save(
            MatchRecord(
                job_id=job_id,
                profile_version="1",
                score=60 + n,
                decision=decision,
                rule="strong",
                hidden_because=None,
                reasons=(),
                notes=(),
                ai_used=False,
            )
        )
        clock.advance(60)
        chats.set_job_status(job_id, status, None)
    assert matches.recent_choices(10) == [(True, 65), (False, 61), (True, 60)]  # newest first
    assert matches.recent_choices(1) == [(True, 65)]


async def test_the_matcher_uses_the_learned_line(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock
) -> None:
    saved = job_store.save(make_job())
    strict = Thresholds(alert_at=100, ai_from=90)  # nothing can reach an alert
    matcher = Matcher(
        job_store, MatchStore(conn, clock=clock), queue, make_profile, learned=lambda: strict, clock=clock
    )
    await matcher.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    record = matcher.matches.get(saved.job_id)
    assert record is not None
    assert record.decision != "alert"  # the same job alerts with the default line (see test_funnel)
