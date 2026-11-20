import pytest

from sarthi.jobs.requirements import min_years_required


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("5+ years of experience in Python", 5),
        ("3-5 years experience", 3),
        ("3–5 years of experience", 3),
        ("Minimum 2 yrs of backend development experience", 2),
        ("4 plus years of hands-on experience", 4),
        ("10+ years of leadership experience preferred. 3+ years of Go experience", 3),
        ("10 years of company history. 3+ years of Go experience", 3),
        ("a 2 year contract", None),
        ("0 years experience", None),
        ("40 years of experience", None),
        ("fresher", None),
        ("", None),
    ],
)
def test_min_years_required(text: str, years: int | None) -> None:
    assert min_years_required(text) == years
