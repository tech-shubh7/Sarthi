from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from ats_scrapers.exceptions import CompanyNotFoundError, ScraperError
from ats_scrapers.models import Job as AtsJob

from sarthi.jobs.models import EmploymentType, RemoteType, SalaryPeriod
from sarthi.queue.tasks import PermanentError, RetryableError
from sarthi.sources import ats
from sarthi.sources.base import SearchQuery
from tests.conftest import FakeClock


def ats_job(**overrides: Any) -> AtsJob:
    data: dict[str, Any] = {
        "url": "https://job-boards.greenhouse.io/acme/jobs/1",
        "title": "Backend Engineer",
        "company": "Acme",
        "ats_type": "greenhouse",
        "ats_id": "1",
    }
    return AtsJob.model_validate(data | overrides)


def test_converts_the_fields_sarthi_uses() -> None:
    posting = ats.to_posting(
        ats_job(
            location="Bengaluru, India",
            country_iso="IN",
            workplace_type="hybrid",
            employment_type="INTERN",
            salary_currency="inr",
            salary_period="YEAR",
            salary_min=600000,
            salary_max=900000,
            apply_url="https://job-boards.greenhouse.io/acme/jobs/1/apply",
            description="Build payment APIs.",
            posted_at=datetime(2026, 11, 19, tzinfo=UTC),
        )
    )
    assert posting.source == "greenhouse"
    assert posting.source_job_id == "1"
    assert posting.country_codes == {"IN"}
    assert posting.remote_type is RemoteType.HYBRID
    assert posting.employment_type is EmploymentType.INTERNSHIP
    assert posting.salary is not None
    assert (posting.salary.currency, posting.salary.period, posting.salary.maximum) == (
        "INR",
        SalaryPeriod.YEAR,
        900000,
    )
    assert str(posting.apply_url).endswith("/apply")


def test_missing_details_stay_unknown() -> None:
    posting = ats.to_posting(ats_job(is_remote=True, salary_min=10, salary_currency=None))
    assert posting.remote_type is RemoteType.REMOTE
    assert posting.salary is None
    assert posting.country_codes == frozenset()


class FakeScraper:
    def __init__(self, result: list[AtsJob] | Exception) -> None:
        self.result = result

    async def afetch(self) -> list[AtsJob]:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> Any:
    def install(result: list[AtsJob] | Exception) -> ats.CompanyBoardSource:
        monkeypatch.setattr(ats, "get_scraper", lambda system, slug: FakeScraper(result))
        return ats.CompanyBoardSource("greenhouse", "acme", clock=clock)

    return install


async def test_fetch_returns_postings_and_filters_by_age(board: Any, clock: FakeClock) -> None:
    jobs = [
        ats_job(ats_id="new", url="https://x.io/j/1", posted_at=datetime(2026, 11, 20, 8, tzinfo=UTC)),
        ats_job(ats_id="old", url="https://x.io/j/2", posted_at=datetime(2026, 11, 10, tzinfo=UTC)),
        ats_job(ats_id="undated", url="https://x.io/j/3"),
    ]
    source = board(jobs)
    assert source.name == "greenhouse:acme"
    assert len(await source.fetch(SearchQuery())) == 3
    recent = await source.fetch(SearchQuery(hours_old=24))
    assert [p.source_job_id for p in recent] == ["new", "undated"]


def _status_error(status: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"retry-after": retry_after} if retry_after else {}
    request = httpx.Request("GET", "https://x.io")
    return httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(status, headers=headers, request=request)
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CompanyNotFoundError("gone"), PermanentError),
        (KeyError("no such system"), PermanentError),
        (_status_error(403), PermanentError),
        (_status_error(503), RetryableError),
        (httpx.ConnectError("down"), RetryableError),
        (ScraperError("parse"), RetryableError),
    ],
)
async def test_errors_map_to_queue_outcomes(board: Any, error: Exception, expected: type[Exception]) -> None:
    with pytest.raises(expected):
        await board(error).fetch(SearchQuery())


async def test_rate_limit_keeps_retry_after(board: Any) -> None:
    with pytest.raises(RetryableError) as caught:
        await board(_status_error(429, retry_after="120")).fetch(SearchQuery())
    assert (caught.value.retry_after, caught.value.status) == (120.0, 429)


def test_supported_systems_include_the_big_ones() -> None:
    systems = ats.supported_systems()
    for name in ("greenhouse", "lever", "ashby", "workday", "darwinbox", "keka"):
        assert name in systems
