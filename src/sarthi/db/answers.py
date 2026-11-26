"""Storage for the answer bank and the questions waiting for the user.

The bank holds every form answer the user gave, reused on later forms; pending questions block one job's
application until the user answers them.
"""

from __future__ import annotations

import json
import sqlite3

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction
from sarthi.jobs.forms import FieldType, FormField

MAX_ANSWER_CHARS = 2000
MAX_OPTIONS = 400  # every country of a "Country / Territory" list (~250), not the first 50 (stopped at Congo)


class AnswerStore:
    """SQL for the ``saved_answer`` and ``pending_question`` tables."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn``; ``clock`` stamps saved answers."""
        self._conn = conn
        self._clock = clock

    # ---- the answer bank --------------------------------------------------------------------------------

    def saved(self) -> dict[str, str]:
        """Every saved question and answer."""
        rows = self._conn.execute("SELECT question, answer FROM saved_answer ORDER BY updated_at")
        return {row["question"]: row["answer"] for row in rows}

    def save(self, question: str, answer: str) -> None:
        """Remember an answer (the latest answer to the same question wins)."""
        question, answer = question.strip()[:500], answer.strip()[:MAX_ANSWER_CHARS]
        if not question or not answer:
            return
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO saved_answer (question, answer, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (question)"
                " DO UPDATE SET answer = excluded.answer, updated_at = excluded.updated_at",
                (question, answer, to_iso(self._clock())),
            )

    def forget(self, question: str) -> bool:
        """Delete one saved answer. True when it existed."""
        with transaction(self._conn) as c:
            return c.execute("DELETE FROM saved_answer WHERE question = ?", (question,)).rowcount > 0

    # ---- questions waiting for the user -------------------------------------------------------------------

    def set_pending(self, job_id: str, fields: list[FormField]) -> None:
        """Replace the questions the user must answer before this job's application continues."""
        with transaction(self._conn) as c:
            c.execute("DELETE FROM pending_question WHERE job_id = ?", (job_id,))
            c.executemany(
                "INSERT OR IGNORE INTO pending_question"
                " (job_id, field_key, label, field_type, options, position) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (job_id, f.key, f.label[:500], f.type, json.dumps(list(f.options)[:MAX_OPTIONS]), i)
                    for i, f in enumerate(fields)
                ],
            )

    def pending(self, job_id: str) -> list[FormField]:
        """The questions still waiting, in form order."""
        rows = self._conn.execute(
            "SELECT field_key, label, field_type, options FROM pending_question"
            " WHERE job_id = ? ORDER BY position",
            (job_id,),
        )
        return [
            FormField(
                row["field_key"],
                row["label"],
                _field_type(row["field_type"]),
                True,
                tuple(json.loads(row["options"])),
            )
            for row in rows
        ]

    def answered(self, job_id: str, key: str) -> None:
        """One question is answered."""
        with transaction(self._conn) as c:
            c.execute("DELETE FROM pending_question WHERE job_id = ? AND field_key = ?", (job_id, key))

    def clear(self, job_id: str) -> None:
        """No questions wait for this job any more (e.g. the user cancelled)."""
        with transaction(self._conn) as c:
            c.execute("DELETE FROM pending_question WHERE job_id = ?", (job_id,))


_TYPES: frozenset[str] = frozenset(
    {
        "text",
        "textarea",
        "email",
        "tel",
        "url",
        "number",
        "date",
        "select",
        "radio",
        "checkbox",
        "combobox",
        "file",
    }
)


def _field_type(value: str) -> FieldType:
    return value if value in _TYPES else "unknown"  # type: ignore[return-value]
