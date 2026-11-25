"""Storage for the durable task queue. The retry policy lives in ``sarthi.queue``; this is only SQL."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction

MAX_ERROR_CHARS = 2000


class TaskStatus(StrEnum):
    """Lifecycle of a task row."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """A claimed task, handed to its handler."""

    id: int
    type: str
    key: str
    payload: dict[str, Any]
    attempts: int  # including the current attempt


class TaskStore:
    """SQL for the ``task`` table. All writes are atomic; every method is safe to call twice."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn`` for all queries; ``clock`` decides what is due."""
        self._conn = conn
        self._clock = clock

    def _now(self) -> str:
        return to_iso(self._clock())

    def enqueue(
        self,
        type_: str,
        key: str,
        payload: dict[str, Any] | None = None,
        run_after: str = "",
        priority: int = 0,
    ) -> None:
        """Insert a task, or merge into the existing ``(type, key)`` row.

        - pending: payload replaced, keeps the earlier ``run_after``, priority only ever rises
        - running: payload replaced and the task runs once more after the current attempt
        - done / failed: reset to pending with a fresh attempt count
        """
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO task (type, key, payload, status, attempts, run_after, rerun, priority) "
                "VALUES (:type, :key, :payload, 'pending', 0, :run_after, 0, :priority) "
                "ON CONFLICT (type, key) DO UPDATE SET"
                " payload = excluded.payload,"
                " priority = max(priority, excluded.priority),"
                " run_after = CASE status"
                "   WHEN 'pending' THEN min(run_after, excluded.run_after)"
                "   WHEN 'running' THEN run_after"
                "   ELSE excluded.run_after END,"
                " attempts = CASE WHEN status IN ('done', 'failed') THEN 0 ELSE attempts END,"
                " last_error = CASE WHEN status IN ('done', 'failed') THEN NULL ELSE last_error END,"
                " rerun = CASE WHEN status = 'running' THEN 1 ELSE rerun END,"
                " status = CASE WHEN status IN ('done', 'failed') THEN 'pending' ELSE status END",
                {
                    "type": type_,
                    "key": key,
                    "payload": json.dumps(payload or {}),
                    "run_after": run_after or self._now(),
                    "priority": priority,
                },
            )

    def claim(self, types: list[str] | None = None) -> TaskRecord | None:
        """Atomically take the next ready task (optionally of these types): highest priority, then oldest."""
        params: dict[str, Any] = {"now": self._now()}
        type_filter = ""
        if types:
            names = [f":t{i}" for i in range(len(types))]
            type_filter = f" AND type IN ({', '.join(names)})"
            params |= {f"t{i}": name for i, name in enumerate(types)}
        # type_filter only adds generated placeholder names; every value stays a bound parameter.
        sql = (
            "UPDATE task SET status = 'running', attempts = attempts + 1, started_at = :now "  # noqa: S608
            "WHERE id = (SELECT id FROM task WHERE status = 'pending' AND run_after <= :now"
            f"{type_filter} ORDER BY priority DESC, run_after, id LIMIT 1) "
            "RETURNING id, type, key, payload, attempts"
        )
        with transaction(self._conn) as c:
            row = c.execute(sql, params).fetchone()
        if row is None:
            return None
        return TaskRecord(
            id=row["id"],
            type=row["type"],
            key=row["key"],
            payload=json.loads(row["payload"]),
            attempts=row["attempts"],
        )

    def complete(self, task_id: int) -> None:
        """Mark a running task done, or pending again if it was re-enqueued while running."""
        with transaction(self._conn) as c:
            c.execute(
                "UPDATE task SET"
                " status = CASE WHEN rerun THEN 'pending' ELSE 'done' END,"
                " attempts = CASE WHEN rerun THEN 0 ELSE attempts END,"
                " run_after = CASE WHEN rerun THEN :now ELSE run_after END,"
                " rerun = 0, started_at = NULL, last_error = NULL "
                "WHERE id = :id AND status = 'running'",
                {"id": task_id, "now": self._now()},
            )

    def fail(self, task_id: int, error: str, retry_at: str | None) -> None:
        """Record a failed attempt: back to pending at ``retry_at``, or ``failed`` when None.

        A task re-enqueued while it was running always gets a fresh run instead.
        """
        with transaction(self._conn) as c:
            c.execute(
                "UPDATE task SET"
                " status = CASE WHEN rerun THEN 'pending'"
                "   WHEN :retry_at IS NULL THEN 'failed' ELSE 'pending' END,"
                " run_after = CASE WHEN rerun THEN :now ELSE coalesce(:retry_at, run_after) END,"
                " attempts = CASE WHEN rerun THEN 0 ELSE attempts END,"
                " rerun = 0, started_at = NULL, last_error = :error "
                "WHERE id = :id AND status = 'running'",
                {"id": task_id, "error": error[:MAX_ERROR_CHARS], "retry_at": retry_at, "now": self._now()},
            )

    def defer(self, task_id: int, until: str) -> None:
        """Put a running task back without counting the attempt (e.g. a rate-limit wait)."""
        with transaction(self._conn) as c:
            c.execute(
                "UPDATE task SET status = 'pending', run_after = :until,"
                " attempts = max(attempts - 1, 0), rerun = 0, started_at = NULL "
                "WHERE id = :id AND status = 'running'",
                {"id": task_id, "until": until},
            )

    def reclaim_stuck(self, started_before: str | None, max_attempts: int) -> int:
        """Return tasks left ``running`` by a crash to pending. Returns how many.

        ``started_before`` None: every running row (at startup, when no worker of this process holds one).
        The interrupted attempt counts (``claim`` counted it): a task that crashed the process
        ``max_attempts`` times is failed instead of crashing it forever. A row re-enqueued while it was
        running (``rerun``, e.g. the user tapped Apply again) gets a fresh run with a fresh count.
        """
        with transaction(self._conn) as c:
            cursor = c.execute(
                "UPDATE task SET"
                " status = CASE WHEN NOT rerun AND attempts >= :max THEN 'failed' ELSE 'pending' END,"
                " last_error = CASE WHEN NOT rerun AND attempts >= :max"
                "   THEN 'interrupted: Sarthi stopped during every attempt' ELSE last_error END,"
                " attempts = CASE WHEN rerun THEN 0 ELSE attempts END,"
                " run_after = :now, started_at = NULL, rerun = 0 "
                "WHERE status = 'running' AND (:cutoff IS NULL OR started_at < :cutoff)",
                {"now": self._now(), "cutoff": started_before, "max": max_attempts},
            )
            return cursor.rowcount

    def requeue_failed(self) -> int:
        """Give every failed task a fresh start. Returns how many."""
        with transaction(self._conn) as c:
            cursor = c.execute(
                "UPDATE task SET status = 'pending', attempts = 0, run_after = :now WHERE status = 'failed'",
                {"now": self._now()},
            )
            return cursor.rowcount

    def active(self, type_: str) -> int:
        """How many tasks of ``type_`` are waiting or running."""
        row = self._conn.execute(
            "SELECT count(*) FROM task WHERE type = ? AND status IN ('pending', 'running')", (type_,)
        ).fetchone()
        return int(row[0])

    def task_counts(self) -> dict[TaskStatus, int]:
        """How many tasks are in each status (every status present, zero when none)."""
        counts = dict.fromkeys(TaskStatus, 0)
        for row in self._conn.execute("SELECT status, count(*) AS n FROM task GROUP BY status"):
            counts[TaskStatus(row["status"])] = row["n"]
        return counts
