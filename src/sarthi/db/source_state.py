"""Storage for per-source state: whether the first-run baseline is done, and the source's health."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction
from sarthi.log import redact

MAX_ERROR_CHARS = 1000


@dataclass(frozen=True, slots=True)
class SourceState:
    """What Sarthi remembers about one source."""

    name: str
    baseline_done: bool
    last_checked_at: str | None
    last_ok_at: str | None
    consecutive_failures: int
    last_error: str | None


class SourceStateStore:
    """SQL for the ``source_state`` table."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn`` for all queries; ``clock`` stamps check times."""
        self._conn = conn
        self._clock = clock

    def get(self, name: str) -> SourceState | None:
        """The state of ``name``, or None if it has never been checked."""
        row = self._conn.execute("SELECT * FROM source_state WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        return SourceState(
            name=row["name"],
            baseline_done=bool(row["baseline_done"]),
            last_checked_at=row["last_checked_at"],
            last_ok_at=row["last_ok_at"],
            consecutive_failures=row["consecutive_failures"],
            last_error=row["last_error"],
        )

    def record_success(self, name: str) -> None:
        """A check worked: baseline done, failure streak cleared."""
        now = to_iso(self._clock())
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO source_state (name, baseline_done, last_checked_at, last_ok_at) "
                "VALUES (?, 1, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET baseline_done = 1,"
                " last_checked_at = excluded.last_checked_at,"
                " last_ok_at = excluded.last_ok_at, consecutive_failures = 0, last_error = NULL",
                (name, now, now),
            )

    def record_failure(self, name: str, error: str) -> int:
        """A check failed. Returns the length of the failure streak."""
        now = to_iso(self._clock())
        error = redact(error)[:MAX_ERROR_CHARS]
        with transaction(self._conn) as c:
            row = c.execute(
                "INSERT INTO source_state (name, last_checked_at, consecutive_failures, last_error) "
                "VALUES (?, ?, 1, ?) "
                "ON CONFLICT (name) DO UPDATE SET last_checked_at = excluded.last_checked_at,"
                " consecutive_failures = consecutive_failures + 1, last_error = excluded.last_error "
                "RETURNING consecutive_failures",
                (name, now, error),
            ).fetchone()
        return int(row["consecutive_failures"])
