"""Storage for chat: small app state (paired chat, pause switch) and the user's choice per job."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
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


_AUTO = "auto_apply.job."

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

    def unsent_matches(self, limit: int, profile_version: str | None = None) -> list[DigestItem]:
        """Best matched jobs (alert or digest) the user has not been shown yet.

        With ``profile_version``, only matches made with that profile count (older ones are being redone).
        """
        rows = self._conn.execute(
            "SELECT m.job_id, m.score FROM job_match m LEFT JOIN job_user u ON u.job_id = m.job_id "
            "WHERE m.decision IN ('alert', 'digest') AND u.job_id IS NULL "
            "AND (:version IS NULL OR m.profile_version = :version) "
            "ORDER BY m.score DESC, m.matched_at DESC LIMIT :limit",
            {"version": profile_version, "limit": limit},
        )
        return [DigestItem(row["job_id"], row["score"]) for row in rows]

    def alerted_since(self, since: datetime) -> int:
        """How many instant alerts went out since ``since`` (and still wait for the user's answer)."""
        row = self._conn.execute(
            "SELECT count(*) AS n FROM job_user WHERE status = 'alerted' AND updated_at >= ?",
            (to_iso(since),),
        ).fetchone()
        return int(row["n"])

    def states(self, prefix: str) -> dict[str, str]:
        """Every stored value whose key starts with ``prefix``, by the rest of the key."""
        rows = self._conn.execute(
            "SELECT key, value FROM app_state WHERE substr(key, 1, length(?)) = ?", (prefix, prefix)
        )
        return {str(row["key"])[len(prefix) :]: str(row["value"]) for row in rows}

    def set_auto(self, job_id: str, *, on: bool) -> None:
        """Mark (or unmark) a job for auto-apply: its filled form may be sent without a tap."""
        self.set_state(f"{_AUTO}{job_id}", "1" if on else None)

    def is_auto(self, job_id: str) -> bool:
        """True while a job is marked for auto-apply."""
        return self.get_state(f"{_AUTO}{job_id}") == "1"

    def applying_alive(self, job_id: str, steps: tuple[str, ...]) -> bool:
        """True while an application for this job is under way or settled.

        That is: a step of ``steps`` waiting or running, a question waiting for the user, or an application
        that was sent, handed over, or is filled with its page still open (``apply.pending.<job>``, see
        sarthi.chat.applying).

        False for an Apply whose chain died (e.g. a task that ran out of attempts): it may be started again.
        """
        marks = ", ".join("?" * len(steps))  # only "?" marks go into the SQL: the values are parameters
        row = self._conn.execute(
            f"SELECT EXISTS (SELECT 1 FROM task WHERE key = ? AND type IN ({marks})"  # noqa: S608
            " AND status IN ('pending', 'running'))"
            " OR EXISTS (SELECT 1 FROM pending_question WHERE job_id = ?)"
            " OR EXISTS (SELECT 1 FROM application WHERE job_id = ? AND (status NOT IN ('failed', 'filled')"
            " OR EXISTS (SELECT 1 FROM app_state WHERE key = 'apply.pending.' || ?))) AS alive",
            (job_id, *steps, job_id, job_id, job_id),
        ).fetchone()
        return bool(row["alive"])

    def status_counts(self) -> dict[str, int]:
        """How many jobs are in each user status."""
        rows = self._conn.execute("SELECT status, count(*) AS n FROM job_user GROUP BY status")
        return {row["status"]: row["n"] for row in rows}
