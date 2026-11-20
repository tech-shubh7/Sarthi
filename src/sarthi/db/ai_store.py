"""Storage for the AI layer: cached answers and token usage (for the daily budget)."""

from __future__ import annotations

import sqlite3

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction


class AiStore:
    """SQL for the ``llm_cache`` and ``llm_usage`` tables."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn`` for all queries; ``clock`` stamps entries."""
        self._conn = conn
        self._clock = clock

    def cache_get(self, key: str) -> str | None:
        """A cached answer, or None."""
        row = self._conn.execute("SELECT value FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def cache_put(self, key: str, value: str) -> None:
        """Store (or replace) a cached answer."""
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO llm_cache (key, value, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, created_at = excluded.created_at",
                (key, value, to_iso(self._clock())),
            )

    def record_usage(self, provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        """Log the tokens one call used."""
        with transaction(self._conn) as c:
            c.execute(
                "INSERT INTO llm_usage (provider, model, prompt_tokens, completion_tokens, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (provider, model, prompt_tokens, completion_tokens, to_iso(self._clock())),
            )

    def tokens_since(self, since: str, exclude_providers: frozenset[str] = frozenset()) -> int:
        """Tokens used since ``since`` (ISO text), not counting ``exclude_providers``."""
        names = [f":p{i}" for i in range(len(exclude_providers))]
        params: dict[str, str] = {"since": since}
        params |= {f"p{i}": p for i, p in enumerate(sorted(exclude_providers))}
        exclude = f" AND provider NOT IN ({', '.join(names)})" if names else ""
        # exclude only adds generated placeholder names; every value stays a bound parameter.
        sql = (
            "SELECT coalesce(sum(prompt_tokens + completion_tokens), 0) FROM llm_usage "  # noqa: S608
            f"WHERE at >= :since{exclude}"
        )
        return int(self._conn.execute(sql, params).fetchone()[0])
