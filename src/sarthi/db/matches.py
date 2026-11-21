"""Storage for match results: one row per job, replaced when the job is matched again."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction


@dataclass(frozen=True, slots=True)
class MatchRecord:
    """A stored match result."""

    job_id: str
    profile_version: str
    score: int
    decision: str
    rule: str
    hidden_because: str | None
    reasons: tuple[str, ...]
    notes: tuple[str, ...]
    ai_used: bool
    matched_at: str = ""


class MatchStore:
    """SQL for the ``job_match`` table."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn`` for all queries; ``clock`` stamps match times."""
        self._conn = conn
        self._clock = clock

    def save(self, record: MatchRecord) -> None:
        """Insert or replace the match for ``record.job_id``."""
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO job_match (job_id, profile_version, score, decision, rule, hidden_because,"
                " reasons, notes, ai_used, matched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (job_id) DO UPDATE SET profile_version = excluded.profile_version,"
                " score = excluded.score, decision = excluded.decision, rule = excluded.rule,"
                " hidden_because = excluded.hidden_because, reasons = excluded.reasons,"
                " notes = excluded.notes,"
                " ai_used = excluded.ai_used, matched_at = excluded.matched_at",
                (
                    record.job_id,
                    record.profile_version,
                    record.score,
                    record.decision,
                    record.rule,
                    record.hidden_because,
                    json.dumps(list(record.reasons)),
                    json.dumps(list(record.notes)),
                    int(record.ai_used),
                    to_iso(self._clock()),
                ),
            )

    def get(self, job_id: str) -> MatchRecord | None:
        """The stored match, or None."""
        row = self._conn.execute("SELECT * FROM job_match WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return MatchRecord(
            job_id=row["job_id"],
            profile_version=row["profile_version"],
            score=row["score"],
            decision=row["decision"],
            rule=row["rule"],
            hidden_because=row["hidden_because"],
            reasons=tuple(json.loads(row["reasons"])),
            notes=tuple(json.loads(row["notes"])),
            ai_used=bool(row["ai_used"]),
            matched_at=row["matched_at"],
        )

    def counts(self) -> dict[str, int]:
        """How many jobs got each decision."""
        rows = self._conn.execute("SELECT decision, count(*) AS n FROM job_match GROUP BY decision")
        return {row["decision"]: row["n"] for row in rows}

    def recent_choices(self, limit: int) -> list[tuple[bool, int]]:
        """The user's last ``limit`` Apply / Skip taps: (applied, the job's score), newest first.

        "Later" is not a choice either way, and a job today's rules hide says nothing about the jobs that
        still qualify, so both are left out.
        """
        rows = self._conn.execute(
            "SELECT u.status, m.score FROM job_user u JOIN job_match m ON m.job_id = u.job_id"
            " WHERE u.status IN ('apply', 'applied', 'skipped') AND m.decision != 'hide'"
            " ORDER BY u.updated_at DESC LIMIT ?",
            (limit,),
        )
        return [(row["status"] != "skipped", int(row["score"])) for row in rows]
