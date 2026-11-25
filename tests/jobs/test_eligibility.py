"""Citizenship, clearance and language conditions read from a posting (fictional postings)."""

from __future__ import annotations

import pytest

from sarthi.jobs.eligibility import citizenship_restrictions, language_requirements, spoken_languages


@pytest.mark.parametrize(
    ("text", "country"),
    [
        ("Applicants must be US citizens. Hybrid in Austin.", "US"),
        ("US citizenship is required for this government contract.", "US"),
        ("Australian citizenship required.", "AU"),
        ("This role is only open to British citizens.", "GB"),
        ("You must be a Canadian citizen or permanent resident.", "CA"),
        ("Permanent residency is required.", None),
        ("Requires an active Secret clearance.", None),
        ("Must be eligible for security clearance.", None),
        ("SC clearance needed from day one.", None),
        ("Candidates need DV cleared status.", None),
    ],
)
def test_restrictions_are_found(text: str, country: str | None) -> None:
    found = citizenship_restrictions(text)
    assert found, text
    assert found[0].country == country


@pytest.mark.parametrize(
    "text",
    [
        "Join our citizen developer programme and build low-code apps.",
        "This is a permanent role with a pension.",
        "No security clearance required.",
        "Security clearance is not required for this role.",
        "We welcome US citizens and international applicants alike.",
        "Customs clearance experience is useful for our logistics team.",
        "Open a PR and request reviews; tests are required.",
        "We hire regardless of citizenship.",
    ],
)
def test_no_restriction_is_read_into_ordinary_text(text: str) -> None:
    assert citizenship_restrictions(text) == []


@pytest.mark.parametrize(
    ("text", "language", "level"),
    [
        ("Fluent German required.", "German", 5),
        ("Dutch is a must.", "Dutch", None),
        ("We are looking for an Arabic speaker.", "Arabic", None),
        ("- C1 French", "French", 5),
        ("Requirements: German (B2) and English.", "German", 4),
        ("You must speak Japanese.", "Japanese", None),
        ("Sehr gute Deutschkenntnisse in Wort und Schrift.", "German", 5),
        ("Excellent command of Spanish.", "Spanish", 5),
    ],
)
def test_language_requirements(text: str, language: str, level: int | None) -> None:
    asks = {ask.language: ask.level for ask in language_requirements(text)}
    assert language in asks, text
    assert asks[language] == level


@pytest.mark.parametrize(
    "text",
    [
        "English; German is a plus.",
        "French would be nice to have.",
        "Dutch is an advantage but not required.",
        "Our office dog is a German shepherd.",
        "We cook excellent French cuisine on Fridays.",
        "We expand into German-speaking markets.",
        "Our team includes Arabic speakers from three countries.",
        "Deutsch\nWir suchen einen Backend Engineer (English-speaking team).",
        "German is not required; our working language is English.",
    ],
)
def test_wishes_and_traps_are_not_requirements(text: str) -> None:
    assert [a for a in language_requirements(text) if a.language != "English"] == []


def test_spoken_languages_always_include_english() -> None:
    assert spoken_languages(()) == {"English": None}
    assert spoken_languages(("German B1", "french (fluent)", "Hindi")) == {
        "English": None,
        "German": 3,
        "French": 5,
        "Hindi": None,
    }
