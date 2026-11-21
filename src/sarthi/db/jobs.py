"""Storage for jobs: save with cross-board dedupe, read back as ``JobPosting``.

A job is "already known" when its canonical link was seen before, or when the same company + role was seen
recently in an overlapping country (the same job on LinkedIn, Naukri and the company's own board). Unlike
JobRadar, which merges a role across all locations, Sarthi keeps one role in different countries apart:
"Backend Engineer, Bengaluru" and "Backend Engineer, London" are different opportunities for a user.
"""

from __future__ import annotations

import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.connection import transaction
from sarthi.jobs.dedupe import job_id_for_url, normalise_url, role_key
from sarthi.jobs.models import JobPosting

FULL_TEXT_MAX_CHARS = 12_000
"""Full descriptions kept for the AI, compressed; longer ones are cut (benefits and legal text come last)."""
REPOST_WINDOW = timedelta(days=30)
"""How long a company + role stays "the same job" when seen again on another board."""


@dataclass(frozen=True, slots=True)
class SaveResult:
    """Outcome of saving one posting."""

    job_id: str
    is_new: bool


def _countries_column(codes: frozenset[str]) -> str:
    """Space-delimited codes so ``LIKE '% IN %'`` matches whole codes only."""
    return f" {' '.join(sorted(codes))} " if codes else ""


class JobStore:
    """SQL for the ``job`` and ``job_source`` tables."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utcnow) -> None:
        """Use ``conn`` for all reads and writes; ``clock`` sets first/last-seen times."""
        self._conn = conn
        self._clock = clock

    def save(self, posting: JobPosting, full_text: str = "") -> SaveResult:
        """Store a posting, or attach it to the job it duplicates. ``is_new`` is True only for a new job.

        ``full_text`` is the complete description when ``posting`` holds only a snippet.
        """
        canonical = normalise_url(str(posting.url))
        if canonical is None:
            raise ValueError(f"job URL is not a usable http(s) link: {posting.url}")
        now = to_iso(self._clock())
        key = role_key(posting.company, posting.title)

        with transaction(self._conn) as c:
            seen = c.execute("SELECT job_id FROM job_source WHERE canonical_url = ?", (canonical,)).fetchone()
            if seen is not None:
                self._touch(c, seen["job_id"], canonical, now)
                return SaveResult(seen["job_id"], is_new=False)

            repost_of = self._find_repost(c, key, posting.country_codes) if key else None
            if repost_of is not None:
                c.execute(
                    "INSERT INTO job_source (job_id, source, canonical_url, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (repost_of, posting.source, canonical, now, now),
                )
                c.execute("UPDATE job SET last_seen_at = ? WHERE id = ?", (now, repost_of))
                return SaveResult(repost_of, is_new=False)

            job_id = job_id_for_url(canonical)
            c.execute(
                "INSERT INTO job (id, role_key, canonical_url, source, title, company, country_codes,"
                " remote_type, posting, posted_at, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    key,
                    canonical,
                    posting.source,
                    posting.title,
                    posting.company,
                    _countries_column(posting.country_codes),
                    posting.remote_type.value,
                    posting.model_dump_json(),
                    to_iso(posting.posted_at) if posting.posted_at else None,
                    now,
                    now,
                ),
            )
            c.execute(
                "INSERT INTO job_source (job_id, source, canonical_url, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, posting.source, canonical, now, now),
            )
            text = (full_text or posting.description)[:FULL_TEXT_MAX_CHARS]
            if text:
                c.execute(
                    "INSERT INTO job_text (job_id, text_zlib) VALUES (?, ?)",
                    (job_id, zlib.compress(text.encode("utf-8"))),
                )
            return SaveResult(job_id, is_new=True)

    def get(self, job_id: str) -> JobPosting | None:
        """The stored posting, or None."""
        row = self._conn.execute("SELECT posting FROM job WHERE id = ?", (job_id,)).fetchone()
        return JobPosting.model_validate_json(row["posting"]) if row else None

    def ids_seen_since(self, since: datetime) -> list[str]:
        """Ids of jobs first seen at or after ``since``, newest first."""
        rows = self._conn.execute(
            "SELECT id FROM job WHERE first_seen_at >= ? ORDER BY first_seen_at DESC", (to_iso(since),)
        )
        return [str(row["id"]) for row in rows]

    def ids_active_since(self, since: datetime) -> list[str]:
        """Ids of jobs seen (first time or again) at or after ``since``, most recently seen first."""
        rows = self._conn.execute(
            "SELECT id FROM job WHERE last_seen_at >= ? ORDER BY last_seen_at DESC", (to_iso(since),)
        )
        return [str(row["id"]) for row in rows]

    def role_key_of(self, job_id: str) -> str | None:
        """The job's company+role key (the same role posted twice shares it), or None."""
        row = self._conn.execute("SELECT role_key FROM job WHERE id = ?", (job_id,)).fetchone()
        return str(row["role_key"]) if row and row["role_key"] else None

    def full_text(self, job_id: str) -> str:
        """The complete description saved with the job ("" when none was saved)."""
        row = self._conn.execute("SELECT text_zlib FROM job_text WHERE job_id = ?", (job_id,)).fetchone()
        return zlib.decompress(row["text_zlib"]).decode("utf-8") if row else ""

    def sources(self, job_id: str) -> list[str]:
        """Canonical links this job was seen at, first sighting first."""
        rows = self._conn.execute(
            "SELECT canonical_url FROM job_source WHERE job_id = ? ORDER BY first_seen_at, canonical_url",
            (job_id,),
        )
        return [row["canonical_url"] for row in rows]

    def count(self) -> int:
        """Number of distinct jobs stored."""
        return int(self._conn.execute("SELECT count(*) FROM job").fetchone()[0])

    def _touch(self, c: sqlite3.Connection, job_id: str, canonical: str, now: str) -> None:
        c.execute("UPDATE job_source SET last_seen_at = ? WHERE canonical_url = ?", (now, canonical))
        c.execute("UPDATE job SET last_seen_at = ? WHERE id = ?", (now, job_id))

    def _find_repost(self, c: sqlite3.Connection, key: str, countries: frozenset[str]) -> str | None:
        """A recent job with the same company + role whose countries overlap (or either is unknown)."""
        since = to_iso(self._clock() - REPOST_WINDOW)
        rows = c.execute(
            "SELECT id, country_codes FROM job WHERE role_key = ? AND first_seen_at >= ? "
            "ORDER BY first_seen_at",
            (key, since),
        ).fetchall()
        for row in rows:
            stored = frozenset(row["country_codes"].split())
            if not stored or not countries or stored & countries:
                return str(row["id"])
        return None
