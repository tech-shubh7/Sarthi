import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sarthi.jobs.models import EmploymentType, SalaryPeriod
from sarthi.queue.tasks import PermanentError, RetryableError
from sarthi.sources.aggregators import (
    AdzunaSource,
    JoobleSource,
    adzuna_posting,
    jooble_posting,
    jooble_salary,
)
from sarthi.sources.base import SearchQuery

# The shape Adzuna answers with (recorded sample in StillOpen's fixtures; fictional companies).
ADZUNA = {
    "count": 3,
    "results": [
        {
            "id": "1",
            "title": "Senior <strong>Python</strong> Developer",
            "description": "Build and run <strong>Python</strong> services for a growing team...",
            "created": "2026-10-20T09:00:00Z",
            "redirect_url": "https://www.adzuna.in/land/ad/1?se=abc&v=def",
            "salary_min": 1200000,
            "salary_max": 1800000,
            "salary_is_predicted": "0",
            "contract_time": "full_time",
            "company": {"display_name": "Example Pvt Ltd"},
            "location": {"display_name": "Pune, Maharashtra", "area": ["India", "Maharashtra", "Pune"]},
        },
        {
            "id": "2",
            "title": "Backend Engineer",
            "description": "Work on APIs.",
            "created": "2026-10-19T09:00:00Z",
            "redirect_url": "https://www.adzuna.in/land/ad/2?se=abc&v=def",
            "salary_min": 900000,
            "salary_max": 900000,
            "salary_is_predicted": "1",
            "company": {"display_name": "Sample Corp"},
            "location": {"display_name": "India", "area": ["India"]},
        },
        {"id": "3", "title": "", "redirect_url": "https://www.adzuna.in/land/ad/3"},  # unusable
    ],
}
JOOBLE = {
    "totalCount": 2,
    "jobs": [
        {
            "id": 7,
            "title": "AI Engineer",
            "location": "Bengaluru, Karnataka",
            "snippet": "Build <b>RAG</b> systems.",
            "salary": "₹6,00,000 - ₹9,00,000 a year",
            "type": "Full-time",
            "link": "https://jooble.org/desc/7",
            "company": "Acme AI",
            "updated": "2026-11-20T10:00:00.0000000",
        },
        {"id": 8, "title": "No link", "company": "X"},  # unusable
        "not even an object",
    ],
}
QUERY = SearchQuery(roles=("Backend Engineer", "AI Engineer"), countries=("IN",), hours_old=48)


def serve(body: Any, status: int = 200) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return (
            httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)
        )

    return httpx.MockTransport(handler), seen


def test_adzuna_item_is_cleaned_and_its_salary_in_rupees() -> None:
    job = adzuna_posting(ADZUNA["results"][0], "IN")
    assert job is not None
    assert job.title == "Senior Python Developer"  # no HTML
    assert "<strong>" not in job.description
    assert (job.company, job.location_text, job.country_codes) == (
        "Example Pvt Ltd",
        "Pune, Maharashtra",
        frozenset({"IN"}),
    )
    assert job.salary is not None
    assert (job.salary.currency, job.salary.minimum, job.salary.period) == ("INR", 1200000, SalaryPeriod.YEAR)
    assert job.employment_type is EmploymentType.FULL_TIME
    assert not job.description_complete
    assert job.posted_at is not None


def test_adzuna_predicted_salaries_are_dropped() -> None:
    job = adzuna_posting(ADZUNA["results"][1], "IN")
    assert job is not None
    assert job.salary is None  # Adzuna's guess, not the employer's number


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"id": "x", "title": "Dev", "company": "not a dict", "redirect_url": "https://a.example/1"},
        {"id": "x", "title": "Dev", "company": {"display_name": "A"}, "redirect_url": "not a url"},
        {
            "id": "x",
            "title": "<b></b>",
            "company": {"display_name": "A"},
            "redirect_url": "https://a.example/1",
        },
    ],
)
def test_unusable_adzuna_items_are_skipped(item: dict[str, Any]) -> None:
    assert adzuna_posting(item, "IN") is None


async def test_adzuna_searches_each_role_in_each_served_country() -> None:
    transport, seen = serve(ADZUNA)
    source = AdzunaSource("my-id", SecretStr("my-key"), transport)
    jobs = await source.fetch(SearchQuery(roles=QUERY.roles, countries=("IN", "AE"), hours_old=48))
    assert len(seen) == 2  # India twice (one per role); the UAE is not an Adzuna country
    assert all(r.url.path == "/v1/api/jobs/in/search/1" for r in seen)
    assert seen[0].url.params["what"] == "Backend Engineer"
    assert seen[0].url.params["max_days_old"] == "2"
    assert [j.title for j in jobs] == ["Senior Python Developer", "Backend Engineer"]  # duplicates merged


async def test_adzuna_remote_search_and_no_country() -> None:
    transport, seen = serve(ADZUNA)
    source = AdzunaSource("id", SecretStr("k"), transport)
    await source.fetch(SearchQuery(roles=("AI Engineer",), countries=("IN",), remote=True))
    assert seen[0].url.params["what"] == "AI Engineer remote"
    assert "max_days_old" not in seen[0].url.params
    assert await source.fetch(SearchQuery(roles=("AI Engineer",), countries=())) == []


async def test_adzuna_errors_never_show_the_key() -> None:
    transport, _ = serve({"error": "x"}, status=500)
    with pytest.raises(RetryableError) as caught:
        await AdzunaSource("id", SecretStr("very-secret-key"), transport).fetch(QUERY)
    assert "very-secret-key" not in str(caught.value)


@pytest.mark.parametrize(
    ("text", "currency", "low", "high", "period"),
    [
        ("₹6,00,000 - ₹9,00,000 a year", "INR", 600000, 900000, SalaryPeriod.YEAR),
        ("$50K - $80K", "USD", 50000, 80000, SalaryPeriod.YEAR),
        ("£25 - £30 per hour", "GBP", 25, 30, SalaryPeriod.HOUR),
        ("30,000 to 40,000 a month", "INR", 30000, 40000, SalaryPeriod.MONTH),  # currency from the country
    ],
)
def test_jooble_salary_text(text: str, currency: str, low: float, high: float, period: SalaryPeriod) -> None:
    salary = jooble_salary(text, "IN")
    assert salary is not None
    assert (salary.currency, salary.minimum, salary.maximum, salary.period) == (currency, low, high, period)


@pytest.mark.parametrize("text", ["", "Competitive", "Up to you", "10 - 20 a year"])
def test_jooble_salary_that_cannot_be_read(text: str) -> None:
    assert jooble_salary(text, None) is None  # no number range, or no way to know the currency


def test_jooble_item() -> None:
    job = jooble_posting(JOOBLE["jobs"][0], "IN")  # type: ignore[arg-type]
    assert job is not None
    assert (job.title, job.company, job.source, job.employment_type) == (
        "AI Engineer",
        "Acme AI",
        "jooble",
        EmploymentType.FULL_TIME,
    )
    assert job.description == "Build RAG systems."
    assert job.salary is not None
    assert job.salary.currency == "INR"
    assert jooble_posting(JOOBLE["jobs"][1], "IN") is None  # type: ignore[arg-type]


async def test_jooble_posts_each_search_and_skips_bad_items() -> None:
    transport, seen = serve(JOOBLE)
    jobs = await JoobleSource(SecretStr("k"), transport).fetch(QUERY)
    bodies = [json.loads(r.content) for r in seen]
    assert bodies[0] == {"keywords": "Backend Engineer", "location": "India", "page": "1"}
    assert [j.title for j in jobs] == ["AI Engineer"]


async def test_jooble_without_countries_searches_everywhere() -> None:
    transport, seen = serve(JOOBLE)
    await JoobleSource(SecretStr("k"), transport).fetch(SearchQuery(roles=("AI Engineer",)))
    assert json.loads(seen[0].content)["location"] == ""


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (403, {"x": 1}, PermanentError),
        (404, {"x": 1}, PermanentError),
        (429, {"x": 1}, RetryableError),
        (503, {"x": 1}, RetryableError),
        (200, "<html>error page</html>", RetryableError),
    ],
)
async def test_jooble_errors(status: int, body: Any, error: type[Exception]) -> None:
    transport, _ = serve(body, status)
    with pytest.raises(error) as caught:
        await JoobleSource(SecretStr("secret-in-url"), transport).fetch(QUERY)
    assert "secret-in-url" not in str(caught.value)


async def test_jooble_network_failure_is_temporary() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(RetryableError):
        await JoobleSource(SecretStr("k"), httpx.MockTransport(broken)).fetch(QUERY)


Handler = Callable[[httpx.Request], httpx.Response]
