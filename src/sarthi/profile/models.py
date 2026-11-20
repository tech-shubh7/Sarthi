"""The user's profile: what they want (preferences) and what they bring (resume, skills).

The preference shape combines OpenApply's targets (titles, keywords, excluded companies and keywords, salary
floor) with Openings' free-text sections (``looking_for``, ``ruled_out``) and Sarthi's global fields: home
country, countries the user can work in, countries where no visa is needed.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sarthi.jobs.models import CountryCode, EmploymentType, Salary, Seniority

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class RemotePreference(StrEnum):
    """How the user feels about remote work."""

    OK = "ok"  # remote, hybrid or on-site
    ONLY = "only"  # remote jobs only
    NO = "no"  # on-site or hybrid only


class Preferences(BaseModel):
    """What the user is looking for."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    roles: tuple[ShortText, ...] = Field(min_length=1, description="Target job titles.")
    keywords: tuple[ShortText, ...] = Field(default=(), description="Skills and tools to look for in jobs.")
    home_country: CountryCode = Field(
        description="Where the user lives; decides country-limited remote jobs."
    )
    countries: frozenset[CountryCode] = Field(
        default=frozenset(),
        description="Countries the user would work in on-site or hybrid. Empty: home only.",
    )
    authorized_countries: frozenset[CountryCode] = Field(
        default=frozenset(),
        description="Countries where the user needs no visa sponsorship. Empty: home only.",
    )
    remote: RemotePreference = RemotePreference.OK
    seniority: frozenset[Seniority] = Field(default=frozenset(), description="Wanted levels. Empty: any.")
    employment_types: frozenset[EmploymentType] = frozenset({EmploymentType.FULL_TIME})
    years_experience: float | None = Field(default=None, ge=0, le=60)
    min_salary: Salary | None = None
    exclude_companies: tuple[ShortText, ...] = ()
    exclude_keywords: tuple[ShortText, ...] = ()
    looking_for: str = Field(default="", max_length=2000)
    ruled_out: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _salary_floor_has_a_minimum(self) -> Self:
        if self.min_salary is not None and self.min_salary.minimum is None:
            raise ValueError("min_salary needs a minimum amount")
        return self

    @property
    def work_countries(self) -> frozenset[str]:
        """Countries the user would work in on-site or hybrid (home always included)."""
        return self.countries | {self.home_country}

    @property
    def visa_free_countries(self) -> frozenset[str]:
        """Countries the user can work in without sponsorship (home always included)."""
        return self.authorized_countries | {self.home_country}


class Profile(BaseModel):
    """Everything matching needs about the user. Holds no name, email or phone number."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    preferences: Preferences
    skills: tuple[ShortText, ...] = Field(default=(), description="Skills listed on the resume.")
    resume_text: str = Field(default="", max_length=50_000, description="The master resume as plain text.")

    @property
    def version(self) -> str:
        """Short hash of the profile; matches are re-run when it changes."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:12]
