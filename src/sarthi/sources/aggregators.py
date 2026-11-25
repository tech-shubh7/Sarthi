"""Adzuna and Jooble: big free job aggregators, searched for the user's roles (each needs a free API key).

Adzuna (https://developer.adzuna.com) covers ~20 countries including India; Jooble (https://jooble.org/api/about)
covers most of the world. Both are searched like the job boards: one search per role, in each of the user's
countries.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from sarthi.jobs import countries
from sarthi.jobs.models import EmploymentType, JobPosting, Salary, SalaryPeriod
from sarthi.queue.tasks import PermanentError, RetryableError
from sarthi.sources import web
from sarthi.sources.base import SearchQuery

log = logging.getLogger(__name__)

MAX_ROLES = 5
PER_PAGE = 50
HOURS_PER_DAY = 24
ADZUNA_API = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
JOOBLE_API = "https://jooble.org/api/{key}"
# Countries Adzuna serves (its country code is the ISO code in lower case, "gb" for the UK).
ADZUNA_COUNTRIES = frozenset(
    [
        "AT", "AU", "BE", "BR", "CA", "CH", "DE", "ES", "FR", "GB",
        "IN", "IT", "MX", "NL", "NZ", "PL", "SG", "US", "ZA",
    ]
)  # fmt: skip
CURRENCY = {
    "AT": "EUR", "AU": "AUD", "BE": "EUR", "BR": "BRL", "CA": "CAD", "CH": "CHF", "DE": "EUR", "ES": "EUR",
    "FR": "EUR", "GB": "GBP", "IN": "INR", "IT": "EUR", "MX": "MXN", "NL": "EUR", "NZ": "NZD", "PL": "PLN",
    "SG": "SGD", "US": "USD", "ZA": "ZAR",
}  # fmt: skip
_SYMBOL = {"₹": "INR", "$": "USD", "£": "GBP", "€": "EUR"}
_EMPLOYMENT = {
    "full_time": EmploymentType.FULL_TIME,
    "permanent": EmploymentType.FULL_TIME,
    "part_time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "full-time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "internship": EmploymentType.INTERNSHIP,
    "temporary": EmploymentType.CONTRACT,
}
_DOLLARS = frozenset({"USD", "CAD", "AUD", "SGD", "NZD"})
# The symbol may come before or after the amount ("€45.000 - €60.000", "45.000 € - 60.000 €").
_SALARY_RANGE = re.compile(
    r"(?P<symbol>[₹$£€])?\s*(?P<low>\d[\d,.]*\d|\d)\s*(?P<low_unit>[km])?\s*(?P<symbol_after>[₹$£€])?"
    r"\s*(?:[-\u2013\u2014]+|to)\s*[₹$£€]?\s*(?P<high>\d[\d,.]*\d|\d)\s*(?P<high_unit>[km])?\s*[₹$£€]?"
    r"\s*(?:per\s+|a\s+|an\s+|/)?(?P<per>year|yr|annual|hour|hr|month|week|day)?",
    re.IGNORECASE,
)
_PERIOD = {
    "year": SalaryPeriod.YEAR, "yr": SalaryPeriod.YEAR, "annual": SalaryPeriod.YEAR,
    "hour": SalaryPeriod.HOUR, "hr": SalaryPeriod.HOUR, "month": SalaryPeriod.MONTH,
    "week": SalaryPeriod.WEEK, "day": SalaryPeriod.DAY,
}  # fmt: skip
_UNIT = {"k": 1_000, "m": 1_000_000}


def _text(value: Any, limit: int) -> str:
    return web.plain_text(str(value or ""), limit).replace("\n", " ").strip()


def _salary(currency: str | None, low: Any, high: Any, period: SalaryPeriod) -> Salary | None:
    if currency is None:
        return None  # an amount in an unknown currency is worse than none
    try:
        return Salary(
            currency=currency,
            period=period,
            minimum=float(low) if low not in (None, "") else None,
            maximum=float(high) if high not in (None, "") else None,
        )
    except (ValidationError, ValueError, TypeError):
        return None


def _amount(text: str) -> float:
    """A written amount: "6,00,000", "60,000.50", and the European "60.000" and "60.000,50"."""
    if "," in text and "." in text:
        decimal = "." if text.rfind(".") > text.rfind(",") else ","
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        decimal = ","  # "60.000": sixty thousand, with dots grouping the thousands
    else:
        decimal = "."
    grouping = "," if decimal == "." else "."
    return float(text.replace(grouping, "").replace(decimal, "."))


def jooble_salary(text: str, country: str | None) -> Salary | None:
    """A range like "₹6,00,000 - ₹9,00,000 a year" or "$50K - $80K"; currency from its symbol or country."""
    found = _SALARY_RANGE.search(text or "")
    if not found:
        return None
    low = _amount(found["low"]) * _UNIT.get((found["low_unit"] or "").lower(), 1)
    high = _amount(found["high"]) * _UNIT.get((found["high_unit"] or "").lower(), 1)
    local = CURRENCY.get(country or "")
    symbol = found["symbol"] or found["symbol_after"] or ""
    # "$" in Canada, Australia, Singapore or New Zealand is the local dollar, not the US one.
    currency = local if symbol == "$" and local in _DOLLARS else (_SYMBOL.get(symbol) or local)
    period = _PERIOD.get((found["per"] or "").lower(), SalaryPeriod.YEAR)
    return _salary(currency, low, high, period)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _posting(data: dict[str, Any]) -> JobPosting | None:
    try:
        return JobPosting.model_validate(data)
    except ValidationError:
        return None


def adzuna_posting(item: dict[str, Any], country: str) -> JobPosting | None:
    """One Adzuna result as a posting, or None when unusable."""
    company, location = _object(item.get("company")), _object(item.get("location"))
    predicted = str(item.get("salary_is_predicted") or "0") == "1"
    kind = str(item.get("contract_time") or item.get("contract_type") or "").lower()
    return _posting(
        {
            "source": "adzuna",
            "source_job_id": str(item.get("id") or "") or None,
            "url": item.get("redirect_url"),
            "title": _text(item.get("title"), 300),
            "company": _text(company.get("display_name"), 300),
            "location_text": _text(location.get("display_name"), 300),
            "country_codes": {country},
            "employment_type": _EMPLOYMENT.get(kind, EmploymentType.UNKNOWN),
            "salary": None
            if predicted
            else _salary(
                CURRENCY.get(country), item.get("salary_min"), item.get("salary_max"), SalaryPeriod.YEAR
            ),
            "description": web.plain_text(str(item.get("description") or "")),
            "description_complete": False,  # Adzuna sends a snippet
            "posted_at": web.parse_time(item.get("created")),
        }
    )


def jooble_posting(item: dict[str, Any], country: str | None) -> JobPosting | None:
    """One Jooble result as a posting, or None when unusable."""
    return _posting(
        {
            "source": "jooble",
            "source_job_id": str(item.get("id") or "") or None,
            "url": item.get("link"),
            "title": _text(item.get("title"), 300),
            "company": _text(item.get("company"), 300),
            "location_text": _text(item.get("location"), 300),
            "country_codes": {country} if country else set(),
            "employment_type": _EMPLOYMENT.get(str(item.get("type") or "").lower(), EmploymentType.UNKNOWN),
            "salary": jooble_salary(str(item.get("salary") or ""), country),
            "description": web.plain_text(str(item.get("snippet") or "")),
            "description_complete": False,
            "posted_at": web.parse_time(item.get("updated")),
        }
    )


def _days(query: SearchQuery) -> int | None:
    return max(1, query.hours_old // HOURS_PER_DAY) if query.hours_old else None


def _unique(postings: list[JobPosting]) -> list[JobPosting]:
    seen: dict[str, JobPosting] = {}
    for posting in postings:
        seen.setdefault(str(posting.url), posting)
    return list(seen.values())


class AdzunaSource:
    """Adzuna searches for the user's roles in each of their countries Adzuna serves."""

    name = "boards:adzuna"

    def __init__(
        self, app_id: str, app_key: SecretStr, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Search with the free ``app_id`` and ``app_key`` from developer.adzuna.com."""
        self.app_id, self.app_key, self._transport = app_id, app_key, transport

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """Jobs for each role in each served country (none when no country is served)."""
        served = [c for c in query.countries if c in ADZUNA_COUNTRIES]
        postings: list[JobPosting] = []
        async with web.client(self._transport) as http:
            for country in served:
                for role in query.roles[:MAX_ROLES]:
                    params = {
                        "app_id": self.app_id,
                        "app_key": self.app_key.get_secret_value(),
                        "results_per_page": str(PER_PAGE),
                        "what": f"{role} remote" if query.remote else role,
                        "content-type": "application/json",
                    }
                    if days := _days(query):
                        params["max_days_old"] = str(days)
                    data = await web.fetch_json(
                        http, self.name, ADZUNA_API.format(country=country.lower()), **params
                    )
                    results = data.get("results") if isinstance(data, dict) else None
                    for item in results if isinstance(results, list) else []:
                        if isinstance(item, dict) and (posting := adzuna_posting(item, country)):
                            postings.append(posting)
        return _unique(postings)


class JoobleSource:
    """Jooble searches for the user's roles in each of their countries."""

    name = "boards:jooble"

    def __init__(self, api_key: SecretStr, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """Search with the free key from jooble.org/api/about."""
        self.api_key, self._transport = api_key, transport

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """Jobs for each role in each of the user's countries (anywhere when none is set)."""
        places: list[str | None] = list(query.countries) or [None]
        postings: list[JobPosting] = []
        url = JOOBLE_API.format(key=self.api_key.get_secret_value())
        async with web.client(self._transport) as http:
            for country in places:
                for role in query.roles[:MAX_ROLES]:
                    body: dict[str, str] = {
                        "keywords": f"{role} remote" if query.remote else role,
                        "location": countries.name(country) if country else "",
                        "page": "1",
                    }
                    data = await _post_json(http, self.name, url, body)
                    jobs = data.get("jobs") if isinstance(data, dict) else None
                    for item in jobs if isinstance(jobs, list) else []:
                        if isinstance(item, dict) and (posting := jooble_posting(item, country)):
                            postings.append(posting)
        return _unique(postings)


async def _post_json(http: httpx.AsyncClient, name: str, url: str, body: dict[str, str]) -> Any:
    """POST ``body``; the shared error kinds, and the key never appears in an error (it is in the URL)."""
    try:
        response = await http.post(url, json=body)
    except (httpx.TransportError, TimeoutError) as error:
        raise RetryableError(f"{name}: {type(error).__name__}") from None
    if response.status_code in (401, 403):
        raise PermanentError(f"{name}: the API key was refused")
    if response.status_code == 429 or response.status_code >= 500:  # noqa: PLR2004
        raise RetryableError(f"{name}: HTTP {response.status_code}", status=response.status_code)
    if response.status_code != 200:  # noqa: PLR2004
        raise PermanentError(f"{name}: HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise RetryableError(f"{name}: answer is not JSON") from None
