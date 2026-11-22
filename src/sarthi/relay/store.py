"""The relay's own small database: which Sarthi owns which chat, and the updates waiting for each Sarthi.

Only hashes of secrets and pairing codes are stored. Waiting updates are deleted as soon as their Sarthi has
fetched them (and after a week at the latest); grants (the files and button taps a Sarthi may use) after a
few days.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS instance (
    id INTEGER PRIMARY KEY,
    secret_hash TEXT NOT NULL UNIQUE,
    chat_id INTEGER UNIQUE,
    pair_hash TEXT,
    pair_expires REAL,
    created REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES instance(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_by_instance ON pending (instance_id, id);
CREATE TABLE IF NOT EXISTS grant_ (
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    instance_id INTEGER NOT NULL REFERENCES instance(id) ON DELETE CASCADE,
    at REAL NOT NULL,
    PRIMARY KEY (kind, value)
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

PENDING_KEEP_S = 7 * 86400  # a Sarthi offline for a week loses what was sent meanwhile
GRANT_KEEP_S = 3 * 86400
UNPAIRED_KEEP_S = 86400  # registered but never paired: forgotten after a day
IDLE_KEEP_S = 180 * 86400  # paired but not seen for half a year: forgotten
MAX_BATCH = 100


def secret_hash(secret: str) -> str:
    """How a Sarthi's secret is stored."""
    return hashlib.sha256(secret.encode()).hexdigest()


class RelayStore:
    """SQLite store for the relay (one connection, used from one event loop)."""

    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time) -> None:
        """Open (and create) the database at ``path`` (":memory:" in tests)."""
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._clock = clock

    def add_instance(self, secret: str) -> int:
        """Register a new Sarthi by its secret; returns its id."""
        now = self._clock()
        cur = self.conn.execute(
            "INSERT INTO instance (secret_hash, created, last_seen) VALUES (?, ?, ?)",
            (secret_hash(secret), now, now),
        )
        return int(cur.lastrowid or 0)

    def instance_for(self, secret: str) -> sqlite3.Row | None:
        """The Sarthi with this secret (and marks it as seen), or None."""
        row = self.conn.execute(
            "SELECT * FROM instance WHERE secret_hash = ?", (secret_hash(secret),)
        ).fetchone()
        if row is not None:
            self.conn.execute("UPDATE instance SET last_seen = ? WHERE id = ?", (self._clock(), row["id"]))
        return row if isinstance(row, sqlite3.Row) else None

    def chat_of(self, instance_id: int) -> int | None:
        """The chat paired with this Sarthi, or None."""
        row = self.conn.execute("SELECT chat_id FROM instance WHERE id = ?", (instance_id,)).fetchone()
        return int(row["chat_id"]) if row and row["chat_id"] is not None else None

    def instance_of_chat(self, chat_id: int) -> int | None:
        """The Sarthi paired with this chat, or None."""
        row = self.conn.execute("SELECT id FROM instance WHERE chat_id = ?", (chat_id,)).fetchone()
        return int(row["id"]) if row else None

    def set_pairing(self, instance_id: int, code_hash: str, lifetime_s: float) -> None:
        """The hash of the one-time code this Sarthi shows its user."""
        self.conn.execute(
            "UPDATE instance SET pair_hash = ?, pair_expires = ? WHERE id = ?",
            (code_hash, self._clock() + lifetime_s, instance_id),
        )

    def pair(self, code_hash: str, chat_id: int) -> int | None:
        """Bind ``chat_id`` to the Sarthi showing this code; returns its id, or None for no such code.

        A chat belongs to one Sarthi: pairing a new one (a reinstall, a new computer) releases the old one.
        """
        row = self.conn.execute(
            "SELECT id FROM instance WHERE pair_hash = ? AND pair_expires >= ?", (code_hash, self._clock())
        ).fetchone()
        if row is None:
            return None
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("UPDATE instance SET chat_id = NULL WHERE chat_id = ?", (chat_id,))
            self.conn.execute(
                "UPDATE instance SET chat_id = ?, pair_hash = NULL, pair_expires = NULL WHERE id = ?",
                (chat_id, row["id"]),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return int(row["id"])

    def push(self, instance_id: int, update: dict[str, Any]) -> None:
        """Keep an update for the Sarthi to fetch."""
        self.conn.execute(
            "INSERT INTO pending (instance_id, body, at) VALUES (?, ?, ?)",
            (instance_id, json.dumps(update, separators=(",", ":")), self._clock()),
        )

    def take(self, instance_id: int, offset: int | None) -> list[dict[str, Any]]:
        """Updates from ``offset`` on (the ones before it were handled: deleted), each with the relay's id."""
        if offset is not None:
            self.conn.execute("DELETE FROM pending WHERE instance_id = ? AND id < ?", (instance_id, offset))
        rows = self.conn.execute(
            "SELECT id, body FROM pending WHERE instance_id = ? ORDER BY id LIMIT ?", (instance_id, MAX_BATCH)
        ).fetchall()
        return [{**json.loads(row["body"]), "update_id": int(row["id"])} for row in rows]

    def grant(self, kind: str, value: str, instance_id: int) -> None:
        """Allow this Sarthi to use a file or a button tap its chat sent."""
        self.conn.execute(
            "INSERT OR REPLACE INTO grant_ (kind, value, instance_id, at) VALUES (?, ?, ?, ?)",
            (kind, value, instance_id, self._clock()),
        )

    def granted(self, kind: str, value: str, instance_id: int) -> bool:
        """True when this Sarthi may use that file or button tap."""
        row = self.conn.execute(
            "SELECT 1 FROM grant_ WHERE kind = ? AND value = ? AND instance_id = ?",
            (kind, value, instance_id),
        ).fetchone()
        return row is not None

    def get_state(self, key: str) -> str | None:
        """A value the relay keeps for itself (its update offset)."""
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        """Keep a value for the relay itself."""
        self.conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))

    def clean(self) -> None:
        """Forget old updates and grants, Sarthis never paired, and Sarthis gone for half a year."""
        now = self._clock()
        self.conn.execute("DELETE FROM pending WHERE at < ?", (now - PENDING_KEEP_S,))
        self.conn.execute("DELETE FROM grant_ WHERE at < ?", (now - GRANT_KEEP_S,))
        self.conn.execute(
            "DELETE FROM instance WHERE chat_id IS NULL AND created < ?", (now - UNPAIRED_KEEP_S,)
        )
        self.conn.execute("DELETE FROM instance WHERE last_seen < ?", (now - IDLE_KEEP_S,))

    def close(self) -> None:
        """Close the database."""
        self.conn.close()
