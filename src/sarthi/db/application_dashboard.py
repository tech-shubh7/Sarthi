"""Read-only application queue for the dashboard, including jobs with no application row yet."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from sarthi.db.applications import UNCONFIRMED_PREFIX


class ApplicationView(StrEnum):
    """The next step or final outcome for an application."""

    ALL = "all"
    INPUT = "input"
    CAPTCHA = "captcha"
    MANUAL = "manual"
    SUBMITTED = "submitted"
    VERIFY = "verify"
    QUEUED = "queued"
    STOPPED = "stopped"


class ApplicationSort(StrEnum):
    """The available application sort orders."""

    NEWEST = "newest"
    OLDEST = "oldest"
    COMPANY = "company"
    ROLE = "role"


@dataclass(frozen=True, slots=True)
class ApplicationFilters:
    """Search and sorting applied within an application view."""

    stage: ApplicationView = ApplicationView.ALL
    query: str = ""
    site: str = ""
    sort: ApplicationSort = ApplicationSort.NEWEST

    def parameters(self) -> dict[str, str]:
        """Return bound SQLite parameters, with literal text search."""
        return {
            "stage": self.stage,
            "q": self.query.strip()[:80],
            "site": self.site.strip()[:200],
            "sort": self.sort,
            "unconfirmed": f"{UNCONFIRMED_PREFIX}%",
        }


@dataclass(frozen=True, slots=True)
class ApplicationItem:
    """Information shown on one application row."""

    job_id: str
    title: str
    company: str
    url: str
    site: str
    updated_at: str
    reply_kind: str | None
    stage: ApplicationView
    status: str
    detail: str
    questions: int
    resume: bool
    letter: bool


_BOARD = """
WITH questions AS (
    SELECT job_id, count(*) AS count FROM pending_question GROUP BY job_id
), active AS (
    SELECT t.key AS job_id, max(coalesce(t.started_at, t.run_after)) AS updated_at
    FROM task t
    WHERE t.status IN ('pending', 'running') AND (
        t.type IN ('form_check', 'apply', 'submit')
        OR (t.type IN ('documents', 'documents_ready') AND json_extract(t.payload, '$.apply') = 1)
        OR (t.type = 'auto_apply' AND EXISTS (
            SELECT 1 FROM app_state s WHERE s.key = 'auto_apply.job.' || t.key AND s.value = '1'
        ))
    )
    GROUP BY t.key
), keys AS (
    SELECT job_id FROM application UNION SELECT job_id FROM questions UNION SELECT job_id FROM active
), board AS (
    SELECT keys.job_id, j.title, j.company,
           coalesce(json_extract(j.posting, '$.apply_url'), j.canonical_url) AS url,
           coalesce(a.site, '') AS site, coalesce(a.updated_at, t.updated_at, j.first_seen_at) AS updated_at,
           coalesce(a.status, '') AS status, coalesce(a.detail, '') AS detail,
           coalesce(q.count, 0) AS questions,
           CASE
               WHEN a.status IN ('submitted', 'applied_by_user') THEN 'submitted'
               WHEN a.status = 'needs_human' AND (
                   a.detail LIKE :unconfirmed OR lower(a.detail) LIKE '%it may have been sent%'
               ) THEN 'verify'
               WHEN q.count > 0 THEN 'input'
               WHEN t.job_id IS NOT NULL THEN 'queued'
               WHEN a.status = 'filled' THEN 'input'
               WHEN a.status = 'needs_human' AND (
                   lower(a.detail) LIKE '%posting is closed%'
                   OR lower(a.detail) LIKE '%no longer accepting applications%'
               ) THEN 'stopped'
               WHEN a.status = 'needs_human' AND lower(a.detail) LIKE '%captcha%' THEN 'captcha'
               WHEN a.status = 'needs_human' THEN 'manual'
               ELSE 'stopped'
           END AS stage
    FROM keys JOIN job j ON j.id = keys.job_id
    LEFT JOIN application a ON a.job_id = keys.job_id
    LEFT JOIN questions q ON q.job_id = keys.job_id LEFT JOIN active t ON t.job_id = keys.job_id
)
"""


_FILTER = """
WHERE (:stage = 'all' OR b.stage = :stage)
  AND (:q = '' OR instr(lower(b.title), lower(:q)) > 0 OR instr(lower(b.company), lower(:q)) > 0)
  AND (:site = '' OR b.site = :site)
"""


class ApplicationDashboard:
    """Read application status from jobs, tasks, questions, and outcomes."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Use the dashboard read-only connection."""
        self._conn = conn

    def counts(self) -> dict[ApplicationView, int]:
        """Count each outcome once, before pagination."""
        rows = self._conn.execute(
            _BOARD + "SELECT stage, count(*) FROM board GROUP BY stage",  # noqa: S608 (static SQL)
            {"unconfirmed": f"{UNCONFIRMED_PREFIX}%"},
        )
        return {ApplicationView(row[0]): int(row[1]) for row in rows}

    def sites(self) -> list[str]:
        """Every site name available to the site filter."""
        rows = self._conn.execute("SELECT DISTINCT site FROM application WHERE site <> '' ORDER BY site")
        return [str(row[0]) for row in rows]

    def count_filtered(self, filters: ApplicationFilters) -> int:
        """Count rows matching the selected filters before pagination."""
        sql = _BOARD + "SELECT count(*) FROM board b " + _FILTER  # noqa: S608 (static SQL)
        return int(self._conn.execute(sql, filters.parameters()).fetchone()[0])

    def rows(self, filters: ApplicationFilters, limit: int, offset: int) -> list[ApplicationItem]:
        """One page of application outcomes and their latest replies."""
        rows = self._conn.execute(
            _BOARD  # noqa: S608 (static SQL)
            + """
            SELECT b.*, d.resume_file, d.letter_file, (
                SELECT r.kind FROM reply r WHERE r.job_id = b.job_id ORDER BY r.received_at DESC LIMIT 1
            ) AS reply_kind
            FROM board b LEFT JOIN job_document d ON d.job_id = b.job_id
            """
            + _FILTER
            + """
            ORDER BY CASE WHEN :sort = 'company' THEN b.company END COLLATE NOCASE,
                     CASE WHEN :sort = 'role' THEN b.title END COLLATE NOCASE,
                     CASE WHEN :sort = 'oldest' THEN b.updated_at END ASC,
                     b.updated_at DESC, b.job_id
            LIMIT :limit OFFSET :offset
            """,
            {**filters.parameters(), "limit": limit, "offset": offset},
        )
        return [
            ApplicationItem(
                r["job_id"], r["title"], r["company"], r["url"], r["site"], r["updated_at"],
                r["reply_kind"], ApplicationView(r["stage"]), r["status"], r["detail"], r["questions"],
                bool(r["resume_file"]), bool(r["letter_file"]),
            )
            for r in rows
        ]  # fmt: skip
