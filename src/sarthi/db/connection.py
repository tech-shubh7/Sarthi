"""Opening the SQLite database with safe settings, and write transactions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sarthi.db.migrations import migrate

_PRAGMAS = (
    "PRAGMA journal_mode = WAL",  # readers never block the writer
    "PRAGMA synchronous = NORMAL",  # safe with WAL, much faster than FULL
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",  # wait up to 5 s for a lock instead of failing
)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the database at ``path`` and bring its schema up to date.

    The connection is in autocommit mode; group writes with ``transaction``. Use ``":memory:"`` in tests.
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run the block in one write transaction: committed on success, rolled back on any error.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so two writers never deadlock upgrading a read lock.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
