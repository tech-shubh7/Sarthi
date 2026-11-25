from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest

from sarthi.jobs.models import EmploymentType, RemoteType, SalaryPeriod
from sarthi.queue.tasks import RetryableError
from sarthi.sources.base import SearchQuery
from sarthi.sources.boards import JobBoardSource, jobspy_country, to_posting


def row(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "li-1",
        "site": "linkedin",
        "job_url": "https://www.linkedin.com/jobs/view/1",
        "job_url_direct": float("nan"),
        "title": "Senior Backend Engineer",
        "company": "Netwrix",
        "location": "Pune/Pimpri-Chinchwad Area",
        "date_posted": date(2026, 11, 19),
        "job_type": float("nan"),
        "is_remote": False,
        "min_amount": float("nan"),
        "max_amount": float("nan"),
        "currency": None,
        "interval": None,
        "description": None,
    }
    return data | overrides


def test_converts_a_real_shaped_linkedin_row() -> None:
    posting = to_posting(row())
    assert (posting.source, posting.source_job_id, posting.company) == ("linkedin", "li-1", "Netwrix")
    assert posting.posted_at == datetime(2026, 11, 19, tzinfo=UTC)
    assert posting.apply_url is None
    assert posting.salary is None
    assert posting.remote_type is RemoteType.UNKNOWN


def test_salary_type_and_remote() -> None:
    posting = to_posting(
        row(
            min_amount=1_200_000.0,
            max_amount=1_800_000.0,
            currency="inr",
            interval="yearly",
            job_type="fulltime, contract",
            is_remote=True,
        )
    )
    assert posting.salary is not None
    assert (posting.salary.currency, posting.salary.period) == ("INR", SalaryPeriod.YEAR)
    assert posting.employment_type is EmploymentType.FULL_TIME
    assert posting.remote_type is RemoteType.REMOTE


def test_row_without_company_is_rejected() -> None:
    with pytest.raises(ValueError, match="company"):
        to_posting(row(company=float("nan")))


@pytest.mark.parametrize(
    ("code", "name"),
    [
        ("IN", "india"),
        ("GB", "uk"),
        ("US", "usa"),
        ("AE", "united arab emirates"),
        ("BD", None),
        ("ZZ", None),
    ],
)
def test_jobspy_country_names(code: str, name: str | None) -> None:
    assert jobspy_country(code) == name


class FakeScrape:
    def __init__(self, frames: dict[str, pd.DataFrame | Exception]) -> None:
        self.frames = frames
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(kwargs)
        result = self.frames[kwargs["search_term"]]
        if isinstance(result, Exception):
            raise result
        return result


async def test_one_search_per_role_and_country_with_country_boards() -> None:
    scrape = FakeScrape({"backend engineer": pd.DataFrame([row()]), "sre": pd.DataFrame()})
    source = JobBoardSource(scraper=scrape)
    query = SearchQuery(roles=("backend engineer", "sre"), countries=("IN", "BD"), hours_old=24)
    postings = await source.fetch(query)

    assert len(postings) == 2  # backend engineer x (IN, BD); sre found nothing
    india = scrape.calls[0]
    assert india["site_name"] == ["linkedin", "indeed", "naukri"]
    assert (india["location"], india["country_indeed"], india["hours_old"]) == ("India", "india", 24)
    bangladesh = scrape.calls[1]
    assert bangladesh["site_name"] == ["linkedin", "bdjobs"]  # Indeed does not cover Bangladesh


async def test_each_country_gets_boards_jobspy_has_and_only_its_own_local_boards() -> None:
    scrape = FakeScrape({"data engineer": pd.DataFrame()})
    source = JobBoardSource(("linkedin", "indeed", "naukri", "seek"), scraper=scrape)
    query = SearchQuery(roles=("data engineer",), countries=("AU", "GB", "IN", "AE"), hours_old=24)
    await source.fetch(query)

    sites = [(call["site_name"], call["location"], call["country_indeed"]) for call in scrape.calls]
    assert sites == [
        (["linkedin", "indeed"], "Australia", "australia"),  # no Seek in JobSpy: it failed every search
        (["linkedin", "indeed"], "United Kingdom", "uk"),  # Naukri lists Indian jobs only
        (["linkedin", "indeed", "naukri"], "India", "india"),
        (["linkedin", "indeed"], "United Arab Emirates", "united arab emirates"),
        (["bayt"], "UAE", "united arab emirates"),  # Bayt's own name for the country
    ]


async def test_a_failed_search_is_skipped_but_all_failing_retries() -> None:
    scrape = FakeScrape({"a": RuntimeError("blocked"), "b": pd.DataFrame([row()])})
    assert len(await JobBoardSource(scraper=scrape).fetch(SearchQuery(roles=("a", "b")))) == 1

    failing = FakeScrape({"a": RuntimeError("blocked")})
    with pytest.raises(RetryableError, match="all 1 searches failed"):
        await JobBoardSource(scraper=failing).fetch(SearchQuery(roles=("a",)))


async def test_unusable_rows_are_skipped() -> None:
    scrape = FakeScrape({"a": pd.DataFrame([row(), row(company=None, job_url="https://x.io/2")])})
    assert len(await JobBoardSource(scraper=scrape).fetch(SearchQuery(roles=("a",)))) == 1


def test_name_lists_the_boards() -> None:
    assert JobBoardSource(("linkedin", "glassdoor")).name == "boards:linkedin+glassdoor"
