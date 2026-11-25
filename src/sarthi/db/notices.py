"""Storage for notices: what Sarthi told the user, kept for the dashboard's Activity page.

Every message a pipeline step has for the user (documents made, questions waiting, a filled form, how an
application ended, an employer's reply, a follow-up draft, a job source that stopped) is stored here whether
or not Telegram is connected, so the dashboard alone shows everything. Only the newest ``KEEP`` are kept.
"""

from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction

KEEP = 500
MAX_TEXT_CHARS = 4000
_TAG = re.compile(r"<[^>]{0,200}>")


def plain(markup: str) -> str:
    """Telegram HTML as plain text: tags dropped, entities read (the dashboard escapes it again)."""
    return html.unescape(_TAG.sub("", markup)).strip()[:MAX_TEXT_CHARS]


@dataclass(frozen=True, slots=True)
class NoticeRecord:
    """One notice, with its job's title and company when it is about a stored job."""

    id: int
    kind: str
    job_id: str | None
    text: str
    at: str
    title: str = ""
    company: str = ""


class NoticeStore:
    """SQL for the ``notice`` table (reads work on the dashboard's read-only connection)."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn``; ``clock`` stamps new notices."""
        self._conn = conn
        self._clock = clock

    def add(self, kind: str, text: str, job_id: str | None = None) -> None:
        """Store one notice (Telegram HTML or plain text); a job that is not stored is left out."""
        body = plain(text)
        if not body:
            return
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO notice (kind, job_id, text, at)"
                " VALUES (?, (SELECT id FROM job WHERE id = ?), ?, ?)",
                (kind[:40], job_id, body, to_iso(self._clock())),
            )
            c.execute(
                "DELETE FROM notice WHERE id <= (SELECT id FROM notice ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (KEEP,),
            )

    def recent(self, limit: int = 50) -> list[NoticeRecord]:
        """The newest notices first."""
        rows = self._conn.execute(
            "SELECT n.id, n.kind, n.job_id, n.text, n.at, j.title, j.company FROM notice n"
            " LEFT JOIN job j ON j.id = n.job_id ORDER BY n.id DESC LIMIT ?",
            (max(1, min(limit, KEEP)),),
        )
        return [
            NoticeRecord(
                int(r["id"]), r["kind"], r["job_id"], r["text"], r["at"], r["title"] or "", r["company"] or ""
            )
            for r in rows
        ]
