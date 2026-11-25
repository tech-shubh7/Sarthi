"""The retry policy on top of the task table: enqueue, claim, complete, retry, fail, defer."""

from __future__ import annotations

import random
from datetime import timedelta
from enum import IntEnum
from typing import Any

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.tasks import TaskRecord, TaskStore
from sarthi.log import redact

MAX_ATTEMPTS = 5
STUCK_AFTER = timedelta(minutes=10)
_BASE_DELAY_S = 30
_MAX_DELAY_S = 3600
_JITTER = (0.8, 1.2)


class Priority(IntEnum):
    """Lanes: a new-job alert never waits behind slow work such as tailoring or applying.

    ``SOURCE``: reading the job sources never waits behind a backlog of ordinary work (thousands of pages
    to read or jobs to match again), or no new job would arrive for a day; a new job's match still goes first.
    ``APPLY``: an application already started (form check, fill, Submit, a refill) finishes before new alerts
    and new Apply taps take the few user slots, or a busy day would leave every application half done.
    """

    BACKGROUND = -10
    NORMAL = 0
    SOURCE = 50
    ALERT = 100
    APPLY = 150


class RetryableError(Exception):
    """Temporary failure (timeout, 5xx, 429): retry with backoff, or after ``retry_after`` seconds."""

    def __init__(
        self, message: str = "", retry_after: float | None = None, status: int | None = None
    ) -> None:
        """Keep the server's ``Retry-After`` (seconds) and the HTTP status, when known."""
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


class PermanentError(Exception):
    """Retrying will not help (403, 404, invalid input): mark the task failed now."""


class DeferError(Exception):
    """Postpone without counting an attempt, e.g. while a site's rate limit resets."""

    def __init__(self, seconds: float) -> None:
        """Ask to run again after ``seconds``."""
        super().__init__(f"deferred {seconds:.0f}s")
        self.seconds = seconds


def backoff_seconds(attempt: int, rng: random.Random | None = None) -> float:
    """``min(30 s x 4^(n-1), 1 h)`` with +-20% jitter, for attempt n >= 1."""
    base = float(min(_BASE_DELAY_S * 4 ** (max(attempt, 1) - 1), _MAX_DELAY_S))
    jitter: float = (rng or random).uniform(*_JITTER)
    return base * jitter


class TaskQueue:
    """Durable work queue: tasks survive crashes and restarts, and each step retries on its own."""

    def __init__(
        self,
        store: TaskStore,
        clock: Clock = utcnow,
        max_attempts: int = MAX_ATTEMPTS,
        rng: random.Random | None = None,
    ) -> None:
        """Wrap ``store`` with the retry policy. ``clock`` and ``rng`` are injectable for tests."""
        self.store = store
        self._clock = clock
        self.max_attempts = max_attempts
        self._rng = rng or random.Random()  # noqa: S311  (jitter, not cryptography)

    def _at(self, seconds: float = 0) -> str:
        return to_iso(self._clock() + timedelta(seconds=seconds))

    def enqueue(
        self,
        type_: str,
        key: str,
        payload: dict[str, Any] | None = None,
        delay: float = 0,
        priority: int = Priority.NORMAL,
    ) -> None:
        """Add a task (idempotent per ``(type_, key)``), runnable after ``delay`` seconds."""
        self.store.enqueue(type_, key, payload, run_after=self._at(delay), priority=priority)

    def claim(self, types: list[str] | None = None) -> TaskRecord | None:
        """Take the next ready task, or None."""
        return self.store.claim(types)

    def complete(self, task: TaskRecord) -> None:
        """Record success."""
        self.store.complete(task.id)

    def retry(self, task: TaskRecord, error: str, retry_after: float | None = None) -> bool:
        """Schedule another attempt, or fail the task after ``max_attempts``. True if retried."""
        error = redact(error)
        if task.attempts >= self.max_attempts:
            self.store.fail(task.id, error, retry_at=None)
            return False
        delay = retry_after if retry_after is not None else backoff_seconds(task.attempts, self._rng)
        self.store.fail(task.id, error, retry_at=self._at(delay))
        return True

    def fail(self, task: TaskRecord, error: str) -> None:
        """Fail the task now, without retrying."""
        self.store.fail(task.id, redact(error), retry_at=None)

    def defer(self, task: TaskRecord, seconds: float) -> None:
        """Run the task again after ``seconds`` without counting this attempt."""
        self.store.defer(task.id, until=self._at(seconds))

    def active(self, type_: str) -> int:
        """How many tasks of ``type_`` are waiting or running."""
        return self.store.active(type_)

    def reclaim_stuck(self, *, everything: bool = False) -> int:
        """Return tasks left running by a crash to the queue. Returns how many.

        ``everything``: at the start of ``sarthi run``, which owns the database, every running row was left
        by a process that is gone, however recently it started. Otherwise only rows older than
        ``STUCK_AFTER`` (another process, e.g. a running Sarthi beside a catch-up, may hold the rest).
        """
        cutoff = None if everything else to_iso(self._clock() - STUCK_AFTER)
        return self.store.reclaim_stuck(started_before=cutoff, max_attempts=self.max_attempts)

    def requeue_failed(self) -> int:
        """Give every failed task a fresh start. Returns how many."""
        return self.store.requeue_failed()
