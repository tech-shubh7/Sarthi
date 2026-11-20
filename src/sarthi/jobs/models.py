"""The single job format every source is converted into (docs/ARCHITECTURE.md §2.3).

Field design follows ATS-Scrapers' ``Job`` model (ISO country codes, ISO 4217 currency) plus what Sarthi
needs for global matching: where remote work is allowed, visa sponsorship and when Sarthi first saw the job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator

DESCRIPTION_SNIPPET_CHARS = 700
"""Jobs are stored lean (OpenApply's "lean index"); full text is fetched only for jobs that pass matching."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value


CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
AwareDatetime = Annotated[datetime, AfterValidator(_require_aware)]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class RemoteType(StrEnum):
    """Where the work happens."""

    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class Sponsorship(StrEnum):
    """Whether the employer sponsors a work visa."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    """Kind of contract."""

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    FREELANCE = "freelance"
    UNKNOWN = "unknown"


class Seniority(StrEnum):
    """Level, read from the job title."""

    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    PRINCIPAL = "principal"
    MANAGER = "manager"
    UNKNOWN = "unknown"


class SalaryPeriod(StrEnum):
    """What a salary amount is paid per."""

    YEAR = "year"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
    HOUR = "hour"


class Salary(BaseModel):
    """A pay range in one currency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: CurrencyCode
    period: SalaryPeriod = SalaryPeriod.YEAR
    minimum: float | None = Field(default=None, ge=0)
    maximum: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _range_is_ordered(self) -> Self:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("salary minimum is greater than maximum")
        return self


class JobPosting(BaseModel):
    """One job as Sarthi stores it, whatever source it came from.

    ``country_codes`` are the countries the job is located in. ``remote_countries`` are the countries a remote
    job accepts people from; empty with ``remote_type == REMOTE`` means anywhere in the world.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source: NonEmptyText = Field(description="Source id, e.g. 'greenhouse', 'linkedin', 'freehire'.")
    source_job_id: str | None = None
    url: HttpUrl
    apply_url: HttpUrl | None = None

    title: NonEmptyText
    company: NonEmptyText
    location_text: str = Field(default="", max_length=300)
    country_codes: frozenset[CountryCode] = frozenset()
    remote_type: RemoteType = RemoteType.UNKNOWN
    remote_countries: frozenset[CountryCode] = frozenset()

    visa_sponsorship: Sponsorship = Sponsorship.UNKNOWN
    relocation: Sponsorship = Sponsorship.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    seniority: Seniority = Seniority.UNKNOWN
    salary: Salary | None = None

    description: str = ""
    description_complete: bool = Field(
        default=True, description="False when ``description`` is a snippet and the full text must be fetched."
    )

    posted_at: AwareDatetime | None = None
    fetched_at: AwareDatetime = Field(default_factory=_utc_now)

    @property
    def is_remote_anywhere(self) -> bool:
        """True for remote jobs open to people in any country."""
        return self.remote_type is RemoteType.REMOTE and not self.remote_countries
