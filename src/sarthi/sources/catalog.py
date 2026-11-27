"""Every job in ATS-Scrapers' hosted snapshot: ~8 million open jobs at ~192,000 companies, crawled by them.

One computer cannot poll 192k company boards without being blocked, so Sarthi reads ATS-Scrapers' published
snapshot instead (one Parquet file, refreshed about daily). DuckDB fetches only the columns and row groups a
query needs: a 2-day search for one country and a few roles measured about 100 MB and 240 MB of memory,
never the whole 4 GB file (for the time it takes, see ``THREADS``). The query runs only when the snapshot has
changed (the small manifest says when), and its rows go through the same converter as Sarthi's own
company-board reads.

Descriptions are not read: they are stored in blocks of up to ~500 MB each (measured 2026-11-21), more than an
8 GB laptop can hold safely, so the jobs arrive with title, place, salary and links, marked incomplete.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import httpx
from ats_scrapers.models import ATSType
from ats_scrapers.models import Job as AtsJob
from pydantic import ValidationError

from sarthi.clock import Clock, utcnow
from sarthi.jobs import countries
from sarthi.jobs.models import JobPosting
from sarthi.queue.tasks import RetryableError
from sarthi.sources import web
from sarthi.sources.ats import to_posting
from sarthi.sources.base import SearchQuery

log = logging.getLogger(__name__)

MANIFEST = "https://storage.stapply.ai/jobs/v1/manifest.json"
SNAPSHOT = "https://storage.stapply.ai/jobs/v1/all.parquet"
# An 8 GB laptop: a hard cap, DuckDB fails (and the read is retried later) rather than take more.
MEMORY_LIMIT = "400MB"
# The file has no column statistics, so a query reads its columns from all ~520 row groups: thousands of small
# range requests, bound by the network's latency, not its speed or the CPU. Measured 2026-11-24 (8.5 M jobs):
# 2 threads took over 5 minutes and always timed out; 16 took 201 s within the same memory cap.
THREADS = 16
MAX_ROWS = 2000
MAX_ROLES = 8
MIN_LOOK_BACK = timedelta(days=2)  # the snapshot is refreshed about daily; reposts are merged later
INTERRUPT_WAIT_S = 10.0  # how long a stopped scan gets to end
QUERY_TIMEOUT_S = 600.0  # a slow connection still finishes; a stuck one is stopped (and retried later)
EXTENSIONS_ENV = "SARTHI_DUCKDB_EXTENSIONS"
_KNOWN_SYSTEMS = frozenset(str(system) for system in ATSType)
_SYSTEM_NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}", re.IGNORECASE)
_ROLE_STYLE = re.compile(r"(back|front|full)[ -]+(end|stack)")
_TITLE = r"regexp_replace(lower(title), '(back|front|full)[ -]+(end|stack)', '\1\2', 'g')"
COLUMNS = (
    "url", "title", "company", "ats_type", "ats_id", "location", "country_iso", "is_remote", "workplace_type",
    "salary_min", "salary_max", "salary_currency", "salary_period", "employment_type", "apply_url",
)  # fmt: skip


def build_query(query: SearchQuery, since: datetime, snapshot: str) -> tuple[str, list[Any]]:
    """The SQL and its parameters (never formatted into the SQL: role names are the user's text)."""
    roles = [
        _like(_ROLE_STYLE.sub(r"\1\2", role.lower())) for role in query.roles[:MAX_ROLES] if role.strip()
    ]
    params: list[Any] = [snapshot]
    where = ["coalesce(posted_at, cast(source_created_at AS VARCHAR)) >= ?"]
    params.append(since.strftime("%Y-%m-%d"))
    where.append("(" + " OR ".join([f"{_TITLE} LIKE ? ESCAPE '\\'"] * len(roles)) + ")" if roles else "false")
    params += roles
    places: list[str] = []
    for code in query.countries:
        places.append("country_iso = ?")
        params.append(code)
        places.append("(country_iso IS NULL AND lower(location) LIKE ? ESCAPE '\\')")
        params.append(_like(countries.name(code)))
    if query.remote:
        places.append("is_remote")
    if places:
        where.append("(" + " OR ".join(places) + ")")
    columns = ", ".join((*COLUMNS, "posted_at"))
    sql = (
        f"SELECT {columns} FROM read_parquet(?) WHERE {' AND '.join(where)}"  # noqa: S608 (bound values)
        f" ORDER BY coalesce(posted_at, cast(source_created_at AS VARCHAR)) DESC, url LIMIT {MAX_ROWS}"
    )
    return sql, params


def _like(text: str) -> str:
    """``%text%`` for LIKE, with LIKE's own wildcards in the user's text escaped."""
    cleaned = " ".join(text.lower().split())
    return "%" + cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def row_to_posting(row: dict[str, Any]) -> JobPosting | None:
    """One snapshot row as a posting, through the same converter as a company-board read; None if unusable."""
    data = {k: v for k, v in row.items() if v is not None}
    # The snapshot names hiring systems newer than the installed ats-scrapers (PandaPé, CareerPlug, isolved,
    # HiringThing and onlyfy). Their rows share these columns, so the converter treats them as custom.
    system = str(data.get("ats_type") or "")
    if system and system not in _KNOWN_SYSTEMS:
        data["ats_type"] = ATSType.CUSTOM.value
    try:
        job = AtsJob.model_validate(data)
        if job.posted_at is not None and job.posted_at.tzinfo is None:
            job = job.model_copy(update={"posted_at": job.posted_at.replace(tzinfo=UTC)})
        posting = to_posting(job)
    except (ValidationError, ValueError):
        return None
    # model_copy skips validation; safe: a plain bool and a short known-good name. No description was read.
    update: dict[str, Any] = {"description_complete": False}
    if system not in _KNOWN_SYSTEMS and _SYSTEM_NAME.fullmatch(system):
        update["source"] = system
    return posting.model_copy(update=update)


class CatalogSource:
    """The user's roles, in their countries, across ATS-Scrapers' snapshot of every company board."""

    name = "catalog:ats-scrapers"

    def __init__(
        self,
        snapshot: str = SNAPSHOT,
        manifest: str = MANIFEST,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = utcnow,
    ) -> None:
        """Query ``snapshot`` when ``manifest`` says it changed."""
        self.snapshot, self.manifest = snapshot, manifest
        self._transport = transport
        self._clock = clock
        self._read_search: tuple[str, tuple[str, ...], tuple[str, ...], bool, timedelta] | None = None

    async def _version(self) -> str | None:
        async with web.client(self._transport) as http:
            data = await web.fetch_json(http, self.name, self.manifest)
        version = data.get("generated_at") if isinstance(data, dict) else None
        return str(version) if version else None

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """Read the search again when either the snapshot or the search changes."""
        if not query.roles:
            return []
        version = await self._version()
        look_back = max(timedelta(hours=query.hours_old or 0), MIN_LOOK_BACK)
        search = (version, query.roles, query.countries, query.remote, look_back) if version else None
        if search is not None and search == self._read_search:
            return []
        sql, params = build_query(query, self._clock() - look_back, self.snapshot)
        rows = await _run(sql, params)
        postings = [p for p in (row_to_posting(r) for r in rows) if p is not None]
        if len(postings) < len(rows):
            log.info("%s: %d of %d rows were not usable", self.name, len(rows) - len(postings), len(rows))
        self._read_search = search
        return postings


async def _run(sql: str, params: list[Any]) -> list[dict[str, Any]]:
    """Run the query in a worker thread with capped memory; interrupted when it takes too long."""
    # The Docker image ships DuckDB's httpfs extension here: its home folder is a no-exec tmpfs, where a
    # downloaded extension could not be loaded.
    folder = os.environ.get(EXTENSIONS_ENV)
    con = duckdb.connect(config={"extension_directory": folder} if folder else {})
    try:
        con.execute(
            f"SET memory_limit='{MEMORY_LIMIT}'; SET threads={THREADS}; SET preserve_insertion_order=false;"
            # Remote file blocks are otherwise kept in memory as the scan goes, until the limit is reached.
            " SET enable_external_file_cache=false;"
        )

        def work() -> list[dict[str, Any]]:
            cursor = con.execute(sql, params)
            names = [d[0] for d in cursor.description or []]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

        thread = asyncio.ensure_future(asyncio.to_thread(work))
        try:
            return await asyncio.wait_for(asyncio.shield(thread), QUERY_TIMEOUT_S)
        except TimeoutError:
            raise RetryableError("catalog: the snapshot query took too long") from None
        except duckdb.Error as error:
            raise RetryableError(f"catalog: {type(error).__name__}: {str(error)[:200]}") from None
        finally:
            if not thread.done():
                # A timeout, or Sarthi stopping (Ctrl+C cancels this task): stop the scan itself, or its
                # thread runs on for minutes and the program cannot exit until it ends.
                con.interrupt()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(asyncio.shield(thread), INTERRUPT_WAIT_S)
    finally:
        with contextlib.suppress(duckdb.Error):
            con.close()
