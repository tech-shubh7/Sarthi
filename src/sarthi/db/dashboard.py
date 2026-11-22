"""Read-only queries for the local dashboard: what Sarthi found, matched, sent and heard back."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from sarthi.clock import to_iso

MAX_ROWS = 200


@dataclass(frozen=True, slots=True)
class Overview:
    """Counts for the overview page."""

    jobs: int
    decisions: dict[str, int]  # alert / digest / hide
    shown: dict[str, int]  # job_user statuses: alerted, digested, apply, skipped, later, applied
    applications: dict[str, int]  # application statuses
    replies: dict[str, int]  # reply kinds
    tasks_waiting: dict[str, int]  # pending or running tasks by type
    ai_tokens_today: dict[str, int]  # provider -> tokens
    pair_link: str | None = None  # shared bot: the t.me link that connects Telegram, until it is used


@dataclass(frozen=True, slots=True)
class MatchRow:
    """One match worth showing."""

    job_id: str
    title: str
    company: str
    url: str
    score: int
    decision: str
    status: str | None
    first_seen_at: str
    source: str


@dataclass(frozen=True, slots=True)
class ApplicationRow:
    """One application and the latest reply about it."""

    job_id: str
    title: str
    company: str
    url: str
    status: str
    site: str
    updated_at: str
    submitted_at: str | None
    reply_kind: str | None


@dataclass(frozen=True, slots=True)
class SourceRow:
    """One job source's health."""

    name: str
    last_ok_at: str | None
    last_checked_at: str | None
    failures: int
    last_error: str | None


def _counts(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in conn.execute(sql, params)}


class DashboardQueries:
    """SQL for the dashboard. Every query only reads."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Read through ``conn``."""
        self._conn = conn

    def overview(self, today_start: datetime) -> Overview:
        """Totals for the overview page; AI use since ``today_start``."""
        c = self._conn
        return Overview(
            jobs=int(c.execute("SELECT count(*) FROM job").fetchone()[0]),
            decisions=_counts(c, "SELECT decision, count(*) FROM job_match GROUP BY decision"),
            shown=_counts(c, "SELECT status, count(*) FROM job_user GROUP BY status"),
            applications=_counts(c, "SELECT status, count(*) FROM application GROUP BY status"),
            replies=_counts(c, "SELECT kind, count(*) FROM reply WHERE job_id IS NOT NULL GROUP BY kind"),
            tasks_waiting=_counts(
                c, "SELECT type, count(*) FROM task WHERE status IN ('pending', 'running') GROUP BY type"
            ),
            ai_tokens_today=_counts(
                c,
                "SELECT provider, sum(prompt_tokens + completion_tokens) FROM llm_usage WHERE at >= ?"
                " GROUP BY provider",
                (to_iso(today_start),),
            ),
            pair_link=self._pair_link(),
        )

    def _pair_link(self) -> str | None:
        row = self._conn.execute("SELECT value FROM app_state WHERE key = 'telegram.pair_link'").fetchone()
        link = str(row[0]) if row and row[0] else ""
        return link if link.startswith("https://t.me/") else None  # only ever a Telegram link

    def matches(self, decisions: tuple[str, ...], since: datetime, limit: int = MAX_ROWS) -> list[MatchRow]:
        """Matches with these decisions among jobs first seen since ``since``, best first."""
        marks = ", ".join("?" * len(decisions))  # only "?" marks go into the SQL: the values are parameters
        sql = (
            "SELECT j.id, j.title, j.company, j.canonical_url, j.source, j.first_seen_at, m.score,"  # noqa: S608
            " m.decision, u.status FROM job_match m JOIN job j ON j.id = m.job_id"
            " LEFT JOIN job_user u ON u.job_id = j.id"
            f" WHERE m.decision IN ({marks}) AND j.first_seen_at >= ?"
            " ORDER BY m.score DESC, j.first_seen_at DESC LIMIT ?"
        )
        rows = self._conn.execute(
            sql,
            (*decisions, to_iso(since), min(limit, MAX_ROWS)),
        )
        return [
            MatchRow(
                r["id"],
                r["title"],
                r["company"],
                r["canonical_url"],
                int(r["score"]),
                r["decision"],
                r["status"],
                r["first_seen_at"],
                r["source"],
            )
            for r in rows
        ]

    def applications(self, limit: int = MAX_ROWS) -> list[ApplicationRow]:
        """Applications, most recently changed first, each with the latest reply's kind."""
        rows = self._conn.execute(
            "SELECT a.job_id, j.title, j.company, j.canonical_url, a.status, a.site, a.updated_at,"
            " a.submitted_at, (SELECT r.kind FROM reply r WHERE r.job_id = a.job_id"
            " ORDER BY r.received_at DESC LIMIT 1) AS kind"
            " FROM application a JOIN job j ON j.id = a.job_id ORDER BY a.updated_at DESC LIMIT ?",
            (min(limit, MAX_ROWS),),
        )
        return [
            ApplicationRow(
                r["job_id"],
                r["title"],
                r["company"],
                r["canonical_url"],
                r["status"],
                r["site"],
                r["updated_at"],
                r["submitted_at"],
                r["kind"],
            )
            for r in rows
        ]

    def sources(self) -> list[SourceRow]:
        """Every source that was ever read: failing ones first."""
        rows = self._conn.execute(
            "SELECT name, last_ok_at, last_checked_at, consecutive_failures, last_error FROM source_state"
            " ORDER BY consecutive_failures DESC, name"
        )
        return [
            SourceRow(
                r["name"],
                r["last_ok_at"],
                r["last_checked_at"],
                int(r["consecutive_failures"]),
                r["last_error"],
            )
            for r in rows
        ]
