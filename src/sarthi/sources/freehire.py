"""Freehire: one keyless search over a catalogue fed by ~90 hiring systems (Workday, Oracle, iCIMS, ...).

It covers thousands of companies Sarthi has no reader for. Port of JobRadar's ``sources/boards/freehire.py``
(MIT), with its India-only filters replaced by the user's roles, countries and remote preference. One search
per role. A filter the API ignored (``meta.ignored_params``) fails the read, since a dropped filter returns
the whole catalogue and looks like a result. Closed postings and old postings made to look new
(``reality.fake_freshness``) are dropped.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import httpx
from pydantic import ValidationError

from sarthi.jobs.models import JobPosting, RemoteType, Seniority
from sarthi.queue.tasks import RetryableError
from sarthi.sources import web
from sarthi.sources.base import SearchQuery

log = logging.getLogger(__name__)

API = "https://freehire.me/api/v1/jobs/search"
PAGE = 100
MAX_PAGES = 3
DEFAULT_WITHIN_DAYS = 2
MAX_WITHIN_DAYS = 30
HOURS_PER_DAY = 24

_WORK_MODE = {"remote": RemoteType.REMOTE, "hybrid": RemoteType.HYBRID, "onsite": RemoteType.ONSITE}
_SENIORITY = {s.value: s for s in Seniority}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def why_not(job: dict[str, Any]) -> str | None:
    """Why a posting is not worth a look; None when it is."""
    if job.get("closed_at"):
        return "closed"
    reality = _dict(job.get("reality"))
    if reality.get("fake_freshness"):
        return "an old posting made to look new"
    return None


def to_posting(job: dict[str, Any]) -> JobPosting:
    """One Freehire posting. Raises ``ValidationError`` for unusable ones."""
    countries = {str(c).upper() for c in job.get("countries") or () if isinstance(c, str) and len(c) == 2}  # noqa: PLR2004
    enrichment = _dict(job.get("enrichment"))
    remote = _WORK_MODE.get(str(job.get("work_mode") or ""), RemoteType.UNKNOWN)
    data: dict[str, Any] = {
        "source": "freehire",
        "source_job_id": str(job.get("public_slug") or job.get("external_id") or "") or None,
        "url": job.get("url"),
        "title": job.get("title"),
        "company": job.get("company"),
        "location_text": str(job.get("location") or "")[:300],
        "country_codes": countries,
        "remote_type": remote,
        "remote_countries": countries if remote is RemoteType.REMOTE else frozenset(),
        "seniority": _SENIORITY.get(str(enrichment.get("seniority") or ""), Seniority.UNKNOWN),
        "description": web.plain_text(job.get("description")),
        "posted_at": web.parse_time(job.get("posted_at") or job.get("created_at")),
    }
    return JobPosting.model_validate(data)


class FreehireSource:
    """Searches Freehire for the user's roles in the user's countries."""

    name = "search:freehire"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """``transport`` is injectable for tests."""
        self._transport = transport

    @staticmethod
    def _params(role: str, query: SearchQuery, offset: int) -> dict[str, str]:
        days = DEFAULT_WITHIN_DAYS
        if query.hours_old is not None:
            days = max(1, min(MAX_WITHIN_DAYS, math.ceil(query.hours_old / HOURS_PER_DAY)))
        params = {
            "q": role,
            "open_within_days": str(days),
            "reality_exclude": "likely-evergreen",
            "sort": "created_at",
            "order": "desc",
            "limit": str(PAGE),
            "offset": str(offset),
        }
        if query.countries:
            params["countries"] = ",".join(c.lower() for c in query.countries)
        if query.remote:
            params["work_mode"] = "remote"
        return params

    async def _search(self, http: httpx.AsyncClient, role: str, query: SearchQuery) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            body = await web.fetch_json(http, self.name, API, **self._params(role, query, page * PAGE))
            data = _dict(body).get("data")
            meta = _dict(_dict(body).get("meta"))
            if not isinstance(data, list):
                raise RetryableError(f"{self.name}: answer in an unexpected shape")
            if meta.get("ignored_params"):
                raise RetryableError(f"{self.name}: filters ignored by the API: {meta['ignored_params']}")
            jobs.extend(j for j in data if isinstance(j, dict) and j.get("url"))
            if len(data) < PAGE or len(jobs) >= int(meta.get("total") or 0):
                break
        return jobs

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """One search per role; a posting found by two roles is returned once."""
        found: dict[str, dict[str, Any]] = {}
        async with web.client(self._transport) as http:
            for role in query.roles:
                for job in await self._search(http, role, query):
                    found.setdefault(str(job["url"]), job)
        postings: list[JobPosting] = []
        for job in found.values():
            if why_not(job) is not None:
                continue
            try:
                postings.append(to_posting(job))
            except ValidationError as error:
                log.debug("%s: skipped unusable job: %d problem(s)", self.name, error.error_count())
        return postings
