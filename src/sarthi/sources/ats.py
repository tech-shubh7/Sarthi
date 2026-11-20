"""Company job boards on ~95 hiring systems (Greenhouse, Lever, Workday, Darwinbox, ...) via ATS-Scrapers.

One ``CompanyBoardSource`` per company. The library returns every open job; matching filters them later.
Sarthi installs ATS-Scrapers without its ``[scrapers]`` extra, so nothing downloads a browser binary;
the few hiring systems that need one are reported as unavailable instead.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.scrapers import ScraperRegistry, get_scraper
from pydantic import ValidationError

from sarthi.clock import Clock, utcnow
from sarthi.jobs.models import EmploymentType, JobPosting, RemoteType, Salary, SalaryPeriod
from sarthi.queue.tasks import PermanentError, RetryableError
from sarthi.sources.base import SearchQuery

if TYPE_CHECKING:
    from ats_scrapers.models import Job as AtsJob

log = logging.getLogger(__name__)

_TOO_MANY_REQUESTS = 429
_SERVER_ERROR = 500

_REMOTE: dict[str, RemoteType] = {
    "onsite": RemoteType.ONSITE,
    "hybrid": RemoteType.HYBRID,
    "remote": RemoteType.REMOTE,
}
_EMPLOYMENT: dict[str, EmploymentType] = {
    "FULL_TIME": EmploymentType.FULL_TIME,
    "PART_TIME": EmploymentType.PART_TIME,
    "CONTRACT": EmploymentType.CONTRACT,
    "TEMPORARY": EmploymentType.CONTRACT,
    "INTERN": EmploymentType.INTERNSHIP,
}


def supported_systems() -> list[str]:
    """Hiring systems this install can read."""
    return sorted(str(ats) for ats in ScraperRegistry.all())


def _salary(job: AtsJob) -> Salary | None:
    if not job.salary_currency or (job.salary_min is None and job.salary_max is None):
        return None
    try:
        return Salary(
            currency=job.salary_currency.upper(),
            period=SalaryPeriod((job.salary_period or "YEAR").lower()),
            minimum=job.salary_min,
            maximum=job.salary_max,
        )
    except (ValidationError, ValueError):
        return None


def _remote_type(job: AtsJob) -> RemoteType:
    if job.workplace_type in _REMOTE:
        return _REMOTE[job.workplace_type]
    if job.is_remote:
        return RemoteType.REMOTE
    return RemoteType.UNKNOWN


def to_posting(job: AtsJob) -> JobPosting:
    """Convert an ATS-Scrapers job into Sarthi's format. Raises ``ValidationError`` for unusable records."""
    countries = {loc.country_iso for loc in job.locations or () if loc.country_iso}
    if job.country_iso:
        countries.add(job.country_iso)
    data: dict[str, Any] = {
        "source": str(job.ats_type),
        "source_job_id": job.ats_id,
        "url": str(job.url),
        "apply_url": str(job.apply_url) if job.apply_url else None,
        "title": job.title,
        "company": job.company,
        "location_text": (job.location or "")[:300],
        "country_codes": countries,
        "remote_type": _remote_type(job),
        "employment_type": _EMPLOYMENT.get(job.employment_type or "", EmploymentType.UNKNOWN),
        "salary": _salary(job),
        "description": job.description or "",
        "posted_at": job.posted_at,
    }
    return JobPosting.model_validate(data)


class CompanyBoardSource:
    """All open jobs on one company's hiring-system board."""

    def __init__(self, system: str, company_slug: str, clock: Clock = utcnow) -> None:
        """Read ``company_slug`` on ``system`` (an ATS-Scrapers id such as ``"greenhouse"``)."""
        self.system = system
        self.company_slug = company_slug
        self._clock = clock

    @property
    def name(self) -> str:
        """``"<system>:<company>"``."""
        return f"{self.system}:{self.company_slug}"

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """Every open job on the board, limited to ``query.hours_old`` when set."""
        try:
            scraper = get_scraper(self.system, self.company_slug)
            jobs = await scraper.afetch()
        except CompanyNotFoundError as error:
            raise PermanentError(f"{self.name}: company board not found") from error
        except (KeyError, ValueError, ImportError) as error:
            raise PermanentError(f"{self.name}: hiring system not available in this install") from error
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == _TOO_MANY_REQUESTS or status >= _SERVER_ERROR:
                retry_after = error.response.headers.get("retry-after")
                raise RetryableError(
                    f"{self.name}: HTTP {status}",
                    retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
                    status=status,
                ) from error
            raise PermanentError(f"{self.name}: HTTP {status}") from error
        except (httpx.TransportError, ScraperError, TimeoutError) as error:
            raise RetryableError(f"{self.name}: {type(error).__name__}: {error}") from error

        postings: list[JobPosting] = []
        for job in jobs:
            try:
                postings.append(to_posting(job))
            except ValidationError as error:
                log.debug("%s: skipped unusable job %s: %s", self.name, job.url, error.error_count())
        if query.hours_old is None:
            return postings
        since = self._clock() - timedelta(hours=query.hours_old)
        return [p for p in postings if p.posted_at is None or p.posted_at >= since]
