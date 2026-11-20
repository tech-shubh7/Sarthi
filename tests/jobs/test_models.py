from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from sarthi.jobs.models import JobPosting, RemoteType, Salary, SalaryPeriod, says_anywhere


def make(**overrides: Any) -> JobPosting:
    data: dict[str, Any] = {
        "source": "greenhouse",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "title": "Backend Engineer",
        "company": "Acme",
    }
    return JobPosting.model_validate(data | overrides)


def test_minimal_job_has_safe_defaults() -> None:
    job = make()
    assert job.remote_type is RemoteType.UNKNOWN
    assert job.country_codes == frozenset()
    assert job.fetched_at.tzinfo is not None
    assert not job.is_remote_anywhere


def test_text_is_stripped_and_blank_titles_rejected() -> None:
    assert make(title="  Backend Engineer  ").title == "Backend Engineer"
    with pytest.raises(ValidationError):
        make(title="   ")


@pytest.mark.parametrize("codes", [["in"], ["IND"], ["I1"]])
def test_country_codes_must_be_iso_alpha2(codes: list[str]) -> None:
    with pytest.raises(ValidationError):
        make(country_codes=codes)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make(posted_at=datetime(2026, 11, 20, 9, 0))  # noqa: DTZ001
    assert make(posted_at=datetime(2026, 11, 20, 9, 0, tzinfo=UTC)).posted_at is not None


def test_unknown_fields_and_bad_urls_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make(salary_text="10 LPA")
    with pytest.raises(ValidationError):
        make(url="not a url")


def test_salary_range_and_currency() -> None:
    salary = Salary(currency="INR", period=SalaryPeriod.YEAR, minimum=1_000_000, maximum=1_800_000)
    assert make(salary=salary).salary == salary
    with pytest.raises(ValidationError):
        Salary(currency="INR", minimum=2, maximum=1)
    with pytest.raises(ValidationError):
        Salary(currency="rupees")
    with pytest.raises(ValidationError):
        Salary(currency="INR", minimum=-1)


def test_remote_anywhere() -> None:
    assert make(remote_type="remote").is_remote_anywhere
    assert not make(remote_type="remote", remote_countries=["IN"]).is_remote_anywhere


def test_jobs_are_immutable() -> None:
    job = make()
    with pytest.raises(ValidationError):
        job.title = "Other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Remote",
        "REMOTE",
        "Worldwide",
        "Anywhere in the World",
        "Remote (Global)",
        "100% remote",
        "Remote job",
    ],
)
def test_says_anywhere(text: str) -> None:
    assert says_anywhere(text)
    assert make(remote_type="remote", location_text=text).is_remote_anywhere


@pytest.mark.parametrize(
    "text",
    ["Lausanne", "Any State", "CET (+/- 3 hours)", "REMOTE (anywhere, EST hours)", "Europe Only", "US"],
)
def test_restrictions_are_not_anywhere(text: str) -> None:
    assert not says_anywhere(text)
    assert not make(remote_type="remote", location_text=text).is_remote_anywhere
