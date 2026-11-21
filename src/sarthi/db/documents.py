"""Storage for the tailored documents made for a job (the files live in the data directory)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """Where one job's documents are, and what the checks found."""

    job_id: str
    profile_version: str
    resume_file: str
    letter_file: str
    report: dict[str, Any] = field(default_factory=dict)
    made_at: str = ""


class DocumentStore:
    """SQL for the ``job_document`` table."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn``; ``clock`` stamps when documents were made."""
        self._conn = conn
        self._clock = clock

    def save(self, record: DocumentRecord) -> None:
        """Insert or replace the documents for ``record.job_id``."""
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO job_document"
                " (job_id, profile_version, resume_file, letter_file, report, made_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (job_id) DO UPDATE SET "
                "profile_version = excluded.profile_version, resume_file = excluded.resume_file, "
                "letter_file = excluded.letter_file, report = excluded.report, made_at = excluded.made_at",
                (
                    record.job_id,
                    record.profile_version,
                    record.resume_file,
                    record.letter_file,
                    json.dumps(record.report, ensure_ascii=False),
                    to_iso(self._clock()),
                ),
            )

    def get(self, job_id: str) -> DocumentRecord | None:
        """The stored documents, or None."""
        row = self._conn.execute("SELECT * FROM job_document WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return DocumentRecord(
            job_id=row["job_id"],
            profile_version=row["profile_version"],
            resume_file=row["resume_file"],
            letter_file=row["letter_file"],
            report=json.loads(row["report"]),
            made_at=row["made_at"],
        )
