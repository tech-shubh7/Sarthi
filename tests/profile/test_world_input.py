"""Profile answers typed by people around the world: currencies, pay periods, country names in their own
language, and roles in any script (fictional values)."""

from __future__ import annotations

import pytest

from sarthi.jobs.models import SalaryPeriod
from sarthi.matching.decide import Decision
from sarthi.matching.funnel import evaluate
from sarthi.matching.prefilter import title_words
from sarthi.profile.answers import find_salary, parse_countries
from tests.matching.conftest import NOW, make_job, make_profile


@pytest.mark.parametrize(
    ("text", "home", "currency", "period", "amount"),
    [
        ("R$ 8.000 por mês", "BR", "BRL", SalaryPeriod.MONTH, 8000),  # was USD 8,000 a year
        ("50 USD/hour", "US", "USD", SalaryPeriod.HOUR, 50),  # was USD 50,000 a year
        ("¥6,000,000", "JP", "JPY", SalaryPeriod.YEAR, 6_000_000),  # was not understood
        ("৳50,000", "BD", "BDT", SalaryPeriod.YEAR, 50_000),
        ("RM 5000 monthly", "MY", "MYR", SalaryPeriod.MONTH, 5000),
        ("4.500 € pro Monat", "DE", "EUR", SalaryPeriod.MONTH, 4500),
        ("S$6,000/month", "SG", "SGD", SalaryPeriod.MONTH, 6000),
        ("12 LPA", "IN", "INR", SalaryPeriod.YEAR, 1_200_000),  # unchanged
        ("80k GBP", "GB", "GBP", SalaryPeriod.YEAR, 80_000),
        ("€55.000", "DE", "EUR", SalaryPeriod.YEAR, 55_000),
    ],
)
def test_salaries_in_local_currencies_and_periods(
    text: str, home: str, currency: str, period: SalaryPeriod, amount: float
) -> None:
    salary = find_salary(text, home)
    assert salary is not None
    assert (salary.currency, salary.period, salary.minimum) == (currency, period, amount)


@pytest.mark.parametrize(
    ("text", "code"),
    [("Deutschland", "DE"), ("Österreich", "AT"), ("España", "ES"), ("Brasil", "BR"), ("日本", "JP")],
)
def test_countries_in_their_own_language(text: str, code: str) -> None:
    assert parse_countries(text).codes == {code}


@pytest.mark.parametrize(
    ("title", "words"),
    [
        ("Técnico de Enfermagem", ["técnico", "de", "enfermagem"]),  # was "t", "cnico"
        ("Инженер-программист", ["инженер", "программист"]),  # was nothing at all
        ("データサイエンティスト（正社員）", ["データサイエンティスト", "正社員"]),
        ("C++ / C# Developer", ["c++", "c#", "engineer"]),
    ],
)
def test_title_words_in_every_script(title: str, words: list[str]) -> None:
    assert title_words(title) == words


async def test_a_role_written_in_japanese_finds_japanese_jobs() -> None:
    tokyo = make_profile(roles=["データサイエンティスト"], home_country="JP", keywords=[])
    job = make_job(title="データサイエンティスト（正社員）", country_codes=["JP"], description="データ分析")
    result = await evaluate(job, tokyo, NOW)
    assert result.decision is not Decision.HIDE, result.outcome
