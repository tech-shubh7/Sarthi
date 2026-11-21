"""Storage for replies from employers, read from the user's mailbox (each email once)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction


@dataclass(frozen=True, slots=True)
class ReplyRecord:
    """One email about an application (``job_id`` None: job-related, but no application matched)."""

    message_id: str
    job_id: str | None
    kind: str
    sender: str = ""
    subject: str = ""
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    received_at: datetime | None = None


class ReplyStore:
    """SQL for the ``reply`` table."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn``; ``clock`` stamps when an email was read."""
        self._conn = conn
        self._clock = clock

    def seen(self, message_id: str) -> bool:
        """True when this email was read before."""
        return (
            self._conn.execute("SELECT 1 FROM reply WHERE message_id = ?", (message_id,)).fetchone()
            is not None
        )

    def save(self, reply: ReplyRecord) -> bool:
        """Store a reply once. False when this email was already stored."""
        with transaction(self._conn) as c:
            cursor = c.execute(
                "INSERT OR IGNORE INTO reply"
                " (message_id, job_id, kind, sender, subject, summary, details, received_at, read_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reply.message_id[:300],
                    reply.job_id,
                    reply.kind,
                    reply.sender[:200],
                    reply.subject[:300],
                    reply.summary[:500],
                    json.dumps(reply.details, ensure_ascii=False)[:5000],
                    to_iso(reply.received_at) if reply.received_at else None,
                    to_iso(self._clock()),
                ),
            )
            return cursor.rowcount > 0

    def heard_from(self, job_id: str) -> bool:
        """True when the employer wrote about this application (anything but noise)."""
        row = self._conn.execute(
            "SELECT 1 FROM reply WHERE job_id = ? AND kind != 'noise' LIMIT 1", (job_id,)
        ).fetchone()
        return row is not None

    def latest_kind(self, job_id: str) -> str | None:
        """The newest reply's kind for this application, or None."""
        row = self._conn.execute(
            "SELECT kind FROM reply WHERE job_id = ? AND kind != 'noise'"
            " ORDER BY coalesce(received_at, read_at) DESC, rowid DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return str(row["kind"]) if row else None
