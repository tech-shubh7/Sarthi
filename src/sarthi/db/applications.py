"""Storage for applications: how each one went, and how many went to a site today (daily limits)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction


class ApplicationStatus(StrEnum):
    """Where an application stands."""

    FILLED = "filled"  # filled, waiting for the user's approval
    SUBMITTED = "submitted"  # Sarthi submitted it and saw the confirmation
    NEEDS_HUMAN = "needs_human"  # handed to the user (CAPTCHA, login, unknown form)
    FAILED = "failed"
    APPLIED_BY_USER = "applied_by_user"  # the user applied themselves and said so


SENT = frozenset({ApplicationStatus.SUBMITTED, ApplicationStatus.APPLIED_BY_USER})


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    """One application."""

    job_id: str
    status: ApplicationStatus
    site: str
    detail: str
    screenshot: str
    updated_at: str
    submitted_at: str | None


class ApplicationStore:
    """SQL for the ``application`` table."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn``; ``clock`` stamps changes."""
        self._conn = conn
        self._clock = clock

    def record(
        self,
        job_id: str,
        status: ApplicationStatus,
        *,
        site: str = "",
        detail: str = "",
        screenshot: str = "",
    ) -> None:
        """Insert or update the job's application. The first submission time is kept."""
        now = to_iso(self._clock())
        sent_at = now if status in SENT else None
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO application (job_id, status, site, detail, screenshot, updated_at, submitted_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (job_id) DO UPDATE SET status = excluded.status,"
                " site = CASE WHEN excluded.site = '' THEN site ELSE excluded.site END,"
                " detail = excluded.detail, screenshot = excluded.screenshot,"
                " updated_at = excluded.updated_at,"
                " submitted_at = coalesce(submitted_at, excluded.submitted_at)",
                (job_id, status.value, site[:200], detail[:1000], screenshot[:500], now, sent_at),
            )

    def get(self, job_id: str) -> ApplicationRecord | None:
        """The job's application, or None."""
        row = self._conn.execute("SELECT * FROM application WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return ApplicationRecord(
            row["job_id"],
            ApplicationStatus(row["status"]),
            row["site"],
            row["detail"],
            row["screenshot"],
            row["updated_at"],
            row["submitted_at"],
        )

    def sent_to_site_since(self, site: str, since: datetime) -> int:
        """Applications Sarthi submitted to ``site`` since ``since``."""
        row = self._conn.execute(
            "SELECT count(*) FROM application WHERE site = ? AND status = 'submitted' AND submitted_at >= ?",
            (site, to_iso(since)),
        ).fetchone()
        return int(row[0])

    def sent_for_role(self, role_key: str, except_job: str) -> str | None:
        """Another job of the same role at the same company that was already applied to, or None."""
        row = self._conn.execute(
            "SELECT a.job_id FROM application a JOIN job j ON j.id = a.job_id"
            " WHERE j.role_key = ? AND a.job_id != ?"
            " AND a.status IN ('submitted', 'applied_by_user') LIMIT 1",
            (role_key, except_job),
        ).fetchone()
        return str(row[0]) if row else None
