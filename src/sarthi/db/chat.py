"""Storage for chat: small app state (paired chat, pause switch) and the user's choice per job."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction


class JobStatus(StrEnum):
    """Where a job stands with the user."""

    ALERTED = "alerted"  # sent as an instant alert, no answer yet
    DIGESTED = "digested"  # sent in a digest, no answer yet
    APPLY = "apply"  # user tapped Apply; waiting for the apply engine
    SKIPPED = "skipped"
    LATER = "later"
    APPLIED = "applied"


ANSWERED = frozenset({JobStatus.APPLY, JobStatus.SKIPPED, JobStatus.LATER, JobStatus.APPLIED})


@dataclass(frozen=True, slots=True)
class DigestItem:
    """A matched job not yet shown to the user."""

    job_id: str
    score: int


class ChatStore:
    """SQL for the ``app_state`` and ``job_user`` tables."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn`` for all queries; ``clock`` stamps updates."""
        self._conn = conn
        self._clock = clock

    def get_state(self, key: str) -> str | None:
        """A stored value, or None."""
        row = self._conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str | None) -> None:
        """Store ``value`` under ``key``; None deletes it."""
        with transaction(self._conn) as c:
            if value is None:
                c.execute("DELETE FROM app_state WHERE key = ?", (key,))
            else:
                c.execute(
                    "INSERT INTO app_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def job_status(self, job_id: str) -> JobStatus | None:
        """The user's status for a job, or None when it was never shown."""
        row = self._conn.execute("SELECT status FROM job_user WHERE job_id = ?", (job_id,)).fetchone()
        return JobStatus(row["status"]) if row else None

    def set_job_status(self, job_id: str, status: JobStatus, message_id: int | None = None) -> None:
        """Record a status; ``message_id`` is kept unless a new one is given."""
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO job_user (job_id, status, message_id, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (job_id) DO UPDATE SET status = excluded.status,"
                " message_id = coalesce(excluded.message_id, message_id), updated_at = excluded.updated_at",
                (job_id, status.value, message_id, to_iso(self._clock())),
            )

    def message_id(self, job_id: str) -> int | None:
        """The chat message that shows this job, if any."""
        row = self._conn.execute("SELECT message_id FROM job_user WHERE job_id = ?", (job_id,)).fetchone()
        return int(row["message_id"]) if row and row["message_id"] is not None else None

    def unsent_matches(self, limit: int) -> list[DigestItem]:
        """Best matched jobs (alert or digest) the user has not been shown yet."""
        rows = self._conn.execute(
            "SELECT m.job_id, m.score FROM job_match m LEFT JOIN job_user u ON u.job_id = m.job_id "
            "WHERE m.decision IN ('alert', 'digest') AND u.job_id IS NULL "
            "ORDER BY m.score DESC, m.matched_at DESC LIMIT ?",
            (limit,),
        )
        return [DigestItem(row["job_id"], row["score"]) for row in rows]

    def status_counts(self) -> dict[str, int]:
        """How many jobs are in each user status."""
        rows = self._conn.execute("SELECT status, count(*) AS n FROM job_user GROUP BY status")
        return {row["status"]: row["n"] for row in rows}
