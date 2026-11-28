"""Job boards (LinkedIn, Indeed, Naukri, Glassdoor, ZipRecruiter, Bayt, ...) via JobSpy.

Uses JobSpy's public ``scrape_jobs`` only, one search per (role, country). Boards that need a country
(Indeed, Glassdoor) are skipped for countries JobSpy does not cover. A country's own board is added for that
country only (Naukri for India, Bayt for the Gulf, BDJobs for Bangladesh, HelloWork for France), and never
searched for another country even when listed in the settings.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
from jobspy import scrape_jobs
from jobspy.model import Country, Site
from pydantic import ValidationError

from sarthi.jobs.geo import country_name
from sarthi.jobs.models import EmploymentType, JobPosting, RemoteType, Salary, SalaryPeriod
from sarthi.queue.tasks import RetryableError
from sarthi.sources.base import SearchQuery

log = logging.getLogger(__name__)

DEFAULT_BOARDS: tuple[str, ...] = ("linkedin", "indeed")
# Boards that list one country's (or region's) jobs only. Only sites JobSpy has (its Site enum): one unknown
# name fails every search of the country (Seek, Australia's board, is not one).
LOCAL_BOARDS: dict[str, frozenset[str]] = {
    "naukri": frozenset({"IN"}),
    "bdjobs": frozenset({"BD"}),
    "bayt": frozenset({"AE", "SA", "QA", "KW", "BH", "OM", "EG", "JO", "LB", "MA"}),
    "hellowork": frozenset({"FR"}),
}
COUNTRY_BOARDS: dict[str, tuple[str, ...]] = {
    country: tuple(board for board, places in LOCAL_BOARDS.items() if country in places)
    for country in sorted(frozenset().union(*LOCAL_BOARDS.values()))
}
NEEDS_COUNTRY: frozenset[str] = frozenset({"indeed", "glassdoor"})
JOBSPY_SITES: frozenset[str] = frozenset(site.value for site in Site)
# Bayt's place name where it differs from the country's (its pages are bayt.com/en/uae/jobs/).
_BAYT_PLACE: dict[str, str] = {"AE": "UAE"}

_JOBSPY_COUNTRIES: frozenset[str] = frozenset(c.value.split(",")[0] for c in Country)
_COUNTRY_ALIASES: dict[str, str] = {"GB": "uk", "US": "usa", "TR": "türkiye", "KR": "south korea"}
_PERIODS: dict[str, SalaryPeriod] = {
    "yearly": SalaryPeriod.YEAR,
    "monthly": SalaryPeriod.MONTH,
    "weekly": SalaryPeriod.WEEK,
    "daily": SalaryPeriod.DAY,
    "hourly": SalaryPeriod.HOUR,
}
_EMPLOYMENT: dict[str, EmploymentType] = {
    "fulltime": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERNSHIP,
}

Scraper = Callable[..., pd.DataFrame]


def jobspy_country(code: str) -> str | None:
    """JobSpy's name for an ISO country code, or None when JobSpy does not cover it."""
    name = _COUNTRY_ALIASES.get(code) or country_name(code).lower()
    return name if name in _JOBSPY_COUNTRIES else None


def _present(value: Any) -> Any:
    """None for the many ways pandas says "missing" (None, NaN, NaT)."""
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NaT:
        return None
    return value


def _posted_at(value: Any) -> datetime | None:
    value = _present(value)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def _salary(row: Mapping[str, Any]) -> Salary | None:
    currency = _present(row.get("currency"))
    low, high = _present(row.get("min_amount")), _present(row.get("max_amount"))
    if not currency or (low is None and high is None):
        return None
    try:
        return Salary(
            currency=str(currency).upper(),
            period=_PERIODS.get(str(_present(row.get("interval")) or "yearly"), SalaryPeriod.YEAR),
            minimum=low,
            maximum=high,
        )
    except ValidationError:
        return None


def to_posting(row: Mapping[str, Any]) -> JobPosting:
    """Convert one JobSpy result row. Raises ``ValidationError`` for unusable rows (e.g. no company)."""
    job_type = str(_present(row.get("job_type")) or "").split(",")[0].strip().lower()
    data: dict[str, Any] = {
        "source": str(_present(row.get("site")) or "jobspy"),
        "source_job_id": _present(row.get("id")),
        "url": _present(row.get("job_url")),
        "apply_url": _present(row.get("job_url_direct")),
        "title": _present(row.get("title")),
        "company": _present(row.get("company")),
        "location_text": str(_present(row.get("location")) or "")[:300],
        "remote_type": RemoteType.REMOTE if _present(row.get("is_remote")) is True else RemoteType.UNKNOWN,
        "employment_type": _EMPLOYMENT.get(job_type, EmploymentType.UNKNOWN),
        "salary": _salary(row),
        "description": str(_present(row.get("description")) or ""),
        "posted_at": _posted_at(row.get("date_posted")),
    }
    return JobPosting.model_validate(data)


class JobBoardSource:
    """Searches job boards for the user's roles in the user's countries."""

    def __init__(self, boards: tuple[str, ...] = DEFAULT_BOARDS, scraper: Scraper = scrape_jobs) -> None:
        """Search ``boards`` (JobSpy site names). ``scraper`` is injectable for tests."""
        self.boards = boards
        self._scrape = scraper

    @property
    def name(self) -> str:
        """``"boards:<board>+<board>"``."""
        return "boards:" + "+".join(self.boards)

    def _boards_for(self, country: str | None) -> tuple[str, ...]:
        boards = [*self.boards, *COUNTRY_BOARDS.get(country or "", ())]
        if country is not None and jobspy_country(country) is None:
            boards = [b for b in boards if b not in NEEDS_COUNTRY]
        # A local board elsewhere searches its own country (Naukri is India-wide): not this user's jobs.
        boards = [b for b in boards if b not in LOCAL_BOARDS or (country or "") in LOCAL_BOARDS[b]]
        unknown = [b for b in boards if b not in JOBSPY_SITES]
        if unknown:  # one unknown name would fail the whole search
            log.warning("%s: skipping boards JobSpy does not have: %s", self.name, ", ".join(unknown))
        return tuple(dict.fromkeys(b for b in boards if b in JOBSPY_SITES))

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """Run one search per (role, country). A failed search is logged and skipped; all failing retries."""
        countries: tuple[str | None, ...] = query.countries or (None,)
        searches = [(role, country) for role in query.roles for country in countries]
        postings: list[JobPosting] = []
        failures = 0
        for role, country in searches:
            try:
                frame = await asyncio.to_thread(self._search, role, country, query)
            # JobSpy raises many unrelated exception types (requests, curl_cffi, parsing); one failed
            # search must not stop the others, and all failing is reported as retryable below.
            except Exception as error:  # noqa: BLE001
                failures += 1
                log.warning("%s: search %r in %s failed: %s", self.name, role, country or "anywhere", error)
                continue
            for row in frame.to_dict("records"):
                try:
                    postings.append(to_posting({str(key): value for key, value in row.items()}))
                except ValidationError as error:
                    log.debug("%s: skipped unusable row: %s errors", self.name, error.error_count())
        if searches and failures == len(searches):
            raise RetryableError(f"{self.name}: all {failures} searches failed")
        return postings

    def _search(self, role: str, country: str | None, query: SearchQuery) -> pd.DataFrame:
        boards = self._boards_for(country)
        if not boards:
            return pd.DataFrame()
        location = country_name(country) if country else None
        # Bayt reads the place from its own URL slugs ("uae", not "united-arab-emirates"): searched apart.
        bayt_place = _BAYT_PLACE.get(country or "")
        groups = [(boards, location)]
        if bayt_place and "bayt" in boards:
            rest = tuple(b for b in boards if b != "bayt")
            groups = [(rest, location), (("bayt",), bayt_place)] if rest else [(("bayt",), bayt_place)]
        frames = [
            self._scrape(
                site_name=list(sites),
                search_term=role,
                location=place,
                is_remote=query.remote,
                results_wanted=query.results_per_search,
                hours_old=query.hours_old,
                country_indeed=jobspy_country(country) if country else "usa",
                description_format="markdown",
                # Without it LinkedIn and Naukri rows have no description at all (half the matches shown on
                # 2026-11-28 were titles only); their job pages refuse Sarthi's own page reader later.
                fetch_description=True,
                verbose=0,
            )
            for sites, place in groups
        ]
        return frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
