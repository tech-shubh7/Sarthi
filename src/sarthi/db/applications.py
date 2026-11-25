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
        """Insert or update the job's application. The first submission time is kept.

        An application that went out stays out: once ``submitted`` or ``applied_by_user`` nothing but
        another sent status changes it (a late refill, hand-over or failure must not make it look unsent,
        or the next Apply tap would send it a second time).
        """
        now = to_iso(self._clock())
        sent_at = now if status in SENT else None
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO application (job_id, status, site, detail, screenshot, updated_at, submitted_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (job_id) DO UPDATE SET status = excluded.status,"
                " site = CASE WHEN excluded.site = '' THEN site ELSE excluded.site END,"
                " detail = excluded.detail, screenshot = excluded.screenshot,"
                " updated_at = excluded.updated_at,"
                " submitted_at = coalesce(submitted_at, excluded.submitted_at)"
                " WHERE status NOT IN ('submitted', 'applied_by_user')"  # SENT: a sent row stays sent
                " OR excluded.status IN ('submitted', 'applied_by_user')",
                (job_id, status.value, site[:200], detail[:1000], screenshot[:500], now, sent_at),
            )

    def get(self, job_id: str) -> ApplicationRecord | None:
        """The job's application, or None."""
        row = self._conn.execute("SELECT * FROM application WHERE job_id = ?", (job_id,)).fetchone()
        return None if row is None else self._record(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> ApplicationRecord:
        return ApplicationRecord(
            row["job_id"],
            ApplicationStatus(row["status"]),
            row["site"],
            row["detail"],
            row["screenshot"],
            row["updated_at"],
            row["submitted_at"],
        )

    def sent(self) -> list[ApplicationRecord]:
        """Every application that went out (Sarthi submitted it or the user applied themselves)."""
        rows = self._conn.execute(
            "SELECT * FROM application WHERE status IN ('submitted', 'applied_by_user') ORDER BY submitted_at"
        )
        return [self._record(row) for row in rows]

    def reminded(self, job_id: str) -> None:
        """A follow-up reminder was sent for this application."""
        with transaction(self._conn) as c:
            c.execute(
                "UPDATE application SET reminders = reminders + 1, reminded_at = ? WHERE job_id = ?",
                (to_iso(self._clock()), job_id),
            )

    def reminders(self, job_id: str) -> tuple[int, str | None]:
        """(reminders sent, when the last one was sent)."""
        row = self._conn.execute(
            "SELECT reminders, reminded_at FROM application WHERE job_id = ?", (job_id,)
        ).fetchone()
        return (int(row["reminders"]), row["reminded_at"]) if row else (0, None)

    def sent_to_site_since(self, site: str, since: datetime) -> int:
        """Applications Sarthi submitted to ``site`` since ``since``."""
        row = self._conn.execute(
            "SELECT count(*) FROM application WHERE site = ? AND status = 'submitted' AND submitted_at >= ?",
            (site, to_iso(since)),
        ).fetchone()
        return int(row[0])

    def waiting_for_login(self, host: str) -> list[str]:
        """Jobs on ``host`` (or a subdomain) handed to the user only because they were not logged in there."""
        pattern = "%." + host.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        rows = self._conn.execute(
            "SELECT job_id FROM application WHERE status = 'needs_human'"
            " AND (site = ? OR site LIKE ? ESCAPE '!')"
            " AND (detail LIKE '%logged-in account%' OR detail LIKE '%signed out%'"
            " OR detail LIKE '%needs you to log in%')"
            " ORDER BY updated_at",
            (host, pattern),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def sent_for_role(self, role_key: str, except_job: str) -> str | None:
        """Another job of the same role at the same company that was already applied to, or None."""
        row = self._conn.execute(
            "SELECT a.job_id FROM application a JOIN job j ON j.id = a.job_id"
            " WHERE j.role_key = ? AND a.job_id != ?"
            " AND a.status IN ('submitted', 'applied_by_user') LIMIT 1",
            (role_key, except_job),
        ).fetchone()
        return str(row[0]) if row else None

    def was_sent(self, job_id: str) -> bool:
        """True when this job's own application already went out (Sarthi submitted it or the user did)."""
        found = self.get(job_id)
        return found is not None and found.status in SENT
