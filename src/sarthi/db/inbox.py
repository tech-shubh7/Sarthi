"""Read-only queries for the dashboard's Activity page: what waits for the user, with or without Telegram.

Questions a form asks (``pending_question``), auto-applies in their veto window (an ``auto_apply`` task still
pending for a job marked in ``app_state``), filled forms waiting for Submit and strong matches not answered.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from sarthi.clock import to_iso

MAX_JOBS = 50
_AUTO = "auto_apply.job."  # sarthi.db.chat: a job marked for auto-apply
_PENDING_FORM = "apply.pending."  # sarthi.chat.applying.PENDING: a filled form waiting for Submit


@dataclass(frozen=True, slots=True)
class Question:
    """One question only the user can answer."""

    key: str
    label: str
    type: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QuestionGroup:
    """A job's waiting questions, in form order."""

    job_id: str
    title: str
    company: str
    url: str
    questions: list[Question] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AutoWaiting:
    """A job Sarthi will apply to by itself when its veto window ends (``at``)."""

    job_id: str
    title: str
    company: str
    url: str
    score: int
    at: str


@dataclass(frozen=True, slots=True)
class InboxCounts:
    """How much waits for the user."""

    strong: int = 0  # strong matches (recent) the user has not answered
    questions: int = 0  # jobs whose form asks something
    forms: int = 0  # filled forms waiting for Submit
    auto: int = 0  # auto-applies in their veto window

    @property
    def total(self) -> int:
        """What needs the user's answer (strong matches are only news)."""
        return self.questions + self.forms + self.auto


class InboxQueries:
    """SQL for the Activity page. Every query only reads."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Read through ``conn``."""
        self._conn = conn

    def counts(self, since: datetime) -> InboxCounts:
        """What waits; strong matches among jobs first seen since ``since``."""
        c = self._conn
        strong = c.execute(
            "SELECT count(*) FROM job_match m JOIN job j ON j.id = m.job_id"
            " LEFT JOIN job_user u ON u.job_id = j.id WHERE m.decision = 'alert' AND j.first_seen_at >= ?"
            " AND (u.status IS NULL OR u.status IN ('alerted', 'digested'))",
            (to_iso(since),),
        ).fetchone()[0]
        questions = c.execute("SELECT count(DISTINCT job_id) FROM pending_question").fetchone()[0]
        forms = c.execute(
            "SELECT count(*) FROM app_state WHERE substr(key, 1, length(?)) = ?",
            (_PENDING_FORM, _PENDING_FORM),
        ).fetchone()[0]
        return InboxCounts(int(strong), int(questions), int(forms), len(self.auto_waiting()))

    def questions(self, job_id: str | None = None) -> list[QuestionGroup]:
        """Waiting questions, optionally focused on one job."""
        rows = self._conn.execute(
            "SELECT q.job_id, q.field_key, q.label, q.field_type, q.options, j.title, j.company,"
            " j.canonical_url FROM pending_question q JOIN job j ON j.id = q.job_id"
            " WHERE ? IS NULL OR q.job_id = ? ORDER BY j.first_seen_at, q.job_id, q.position",
            (job_id, job_id),
        )
        groups: dict[str, QuestionGroup] = {}
        for r in rows:
            group = groups.get(r["job_id"])
            if group is None:
                if len(groups) >= MAX_JOBS:
                    break
                group = groups[r["job_id"]] = QuestionGroup(
                    r["job_id"], r["title"], r["company"], r["canonical_url"]
                )
            try:
                options = tuple(str(o) for o in json.loads(r["options"]))
            except ValueError:
                options = ()
            group.questions.append(Question(r["field_key"], r["label"], r["field_type"], options))
        return list(groups.values())

    def auto_waiting(self) -> list[AutoWaiting]:
        """Auto-applies still in their veto window, soonest first."""
        rows = self._conn.execute(
            "SELECT t.key, t.run_after, j.title, j.company, j.canonical_url, coalesce(m.score, 0) AS score"
            " FROM task t JOIN job j ON j.id = t.key LEFT JOIN job_match m ON m.job_id = t.key"
            " LEFT JOIN job_user u ON u.job_id = t.key"
            " WHERE t.type = 'auto_apply' AND t.status = 'pending'"
            " AND (u.status IS NULL OR u.status IN ('alerted', 'digested')"
            " OR (u.status = 'apply' AND json_extract(t.payload, '$.resume') = 1))"
            " AND EXISTS (SELECT 1 FROM app_state s WHERE s.key = ? || t.key AND s.value = '1')"
            " ORDER BY t.run_after LIMIT ?",
            (_AUTO, MAX_JOBS),
        )
        return [
            AutoWaiting(
                r["key"], r["title"], r["company"], r["canonical_url"], int(r["score"]), r["run_after"]
            )
            for r in rows
        ]
