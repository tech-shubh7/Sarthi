# Cases ported from StillOpen (Apache-2.0), tracker/tests_prefs.py and tracker/tests_walkthrough.py @ ce7fb22,
# plus Sarthi's own India cases.
import pytest

from sarthi.jobs import geo
from sarthi.jobs.countries import name


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Malaysia", {"MY"}),
        ("malaysia", {"MY"}),
        ("Kuala Lumpur", {"MY"}),
        ("Kuala Lumpur, Malaysia", {"MY"}),
        ("Dhaka", {"BD"}),
        ("Chattogram", {"BD"}),
        ("UAE", {"AE"}),
        ("United Arab Emirates", {"AE"}),
        ("UK", {"GB"}),
        ("England", {"GB"}),
        ("United States", {"US"}),
        ("USA", {"US"}),
        ("Singapore", {"SG"}),
        ("Viet Nam", {"VN"}),
        ("Türkiye", {"TR"}),
        ("Dublin, Ireland", {"IE"}),
        ("Manchester, United Kingdom", {"GB"}),
        ("Petaling Jaya, Malaysia", {"MY"}),
        # Sarthi additions for India
        ("Bengaluru", {"IN"}),
        ("Bangalore, Karnataka, India", {"IN"}),
        ("Gurugram", {"IN"}),
        ("Ahmedabad", {"IN"}),
        ("Gandhinagar", {"IN"}),
        ("Kochi", {"IN"}),
        ("Remote - India", {"IN"}),
    ],
)
def test_countries_aliases_and_cities(text: str, expected: set[str]) -> None:
    assert geo.resolve_place(text) == expected


def test_regions() -> None:
    assert geo.resolve_place("Gulf") == {"AE", "SA", "QA", "KW", "BH", "OM"}
    assert "MY" in (geo.resolve_place("Southeast Asia") or set())
    assert "DE" in (geo.resolve_place("Europe") or set())
    assert "GB" not in (geo.resolve_place("EU") or set())


@pytest.mark.parametrize(
    "text", ["Anywhere", "worldwide", "Remote worldwide", "Global", "Work from anywhere"]
)
def test_anywhere(text: str) -> None:
    assert geo.resolve_place(text) == {"*"}


@pytest.mark.parametrize("text", ["Atlantis", "", "   ", "Georgia", "uk", "us", "Nowhereville"])
def test_unknown_and_ambiguous_places_are_not_guessed(text: str) -> None:
    assert geo.resolve_place(text) is None


def test_detect_hybrid_with_sponsorship_and_relocation() -> None:
    found = geo.detect(
        "Senior Backend Engineer",
        "Location: Kuala Lumpur, Malaysia\nHybrid. We offer visa sponsorship and a relocation package. "
        "Full-time.",
    )
    assert found["arrangement"] == "hybrid"
    assert (found["country"], found["location_text"]) == ("MY", "Kuala Lumpur, Malaysia")
    assert (found["sponsorship"], found["relocation"], found["employment_type"], found["seniority"]) == (
        "yes",
        "yes",
        "full_time",
        "senior",
    )


def test_location_text_stops_at_the_end_of_the_place() -> None:
    found = geo.detect(
        "Dev", "Location: Kuala Lumpur, Malaysia. Hybrid. Visa sponsorship and relocation package offered."
    )
    assert found["location_text"] == "Kuala Lumpur, Malaysia"
    assert geo.detect("Dev", "Location: Dubai | Full-time")["location_text"] == "Dubai"


def test_remote_worldwide_has_no_country() -> None:
    found = geo.detect("Backend Engineer", "We are fully remote and hire worldwide. Work from anywhere.")
    assert found["arrangement"] == "remote"
    assert found["location_text"] == "Remote (worldwide)"
    assert "country" not in found


def test_negations_and_conflicts() -> None:
    assert geo.detect("Dev", "On-site in Dhaka. This is not a remote position.")["arrangement"] == "onsite"
    assert geo.detect("Dev", "No remote work. Office based in Dhaka.")["arrangement"] == "onsite"
    assert "arrangement" not in geo.detect("Dev", "Remote friendly with an on-site kickoff.")
    assert geo.detect("Dev", "Hybrid with some remote days.")["arrangement"] == "hybrid"


@pytest.mark.parametrize(
    ("text", "field", "value"),
    [
        ("We cannot sponsor visas.", "sponsorship", "no"),
        ("No visa sponsorship is available.", "sponsorship", "no"),
        ("Must have the right to work in the UK.", "sponsorship", "no"),
        ("Visa support is available.", "sponsorship", "yes"),
        ("No relocation assistance.", "relocation", "no"),
    ],
)
def test_sponsorship_and_relocation(text: str, field: str, value: str) -> None:
    assert geo.detect("Dev", text)[field] == value


@pytest.mark.parametrize(
    ("title", "text", "field", "value"),
    [
        ("Software Engineering Intern", "", "employment_type", "internship"),
        ("Backend Developer", "This is a 6 month contract role.", "employment_type", "contract"),
        ("Principal Engineer", "", "seniority", "principal"),
        ("Tech Lead", "", "seniority", "lead"),
        ("Engineering Manager", "", "seniority", "manager"),
    ],
)
def test_type_and_seniority(title: str, text: str, field: str, value: str) -> None:
    assert geo.detect(title, text)[field] == value


def test_seniority_comes_from_title_not_a_stray_word() -> None:
    assert "seniority" not in geo.detect("Developer", "We want a senior mindset.")


def test_a_vague_posting_yields_nothing_rather_than_a_guess() -> None:
    assert geo.detect("Developer", "Great team, great culture.") == {}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bastrop, TX", {"US"}),
        ("Palo Alto, CA", {"US"}),
        ("Remote - United States", {"US"}),
        ("US - Remote", {"US"}),
        ("Petaling Jaya, Malaysia", {"MY"}),
        ("Dhaka", {"BD"}),
        ("Hong Kong; Singapore; Taiwan", {"HK", "SG", "TW"}),
        ("United States and Canada", {"US", "CA"}),
        ("Gurugram, IN", {"IN"}),
        ("Texas", {"US"}),
        ("", set()),
        ("Remote", set()),
        ("Remote, Global", set()),
        ("Anywhere in the World", set()),
        ("Time zone: CET (+/- 3 hours)", set()),
        ("Zug (remote)", set()),
        # Sarthi: real-data cases found in an end-to-end run (2026-11-20)
        ("Toronto, CA", {"CA"}),
        ("Zürich, CH", {"CH"}),
        ("Ontario, CAN", {"CA"}),
        ("Remote, DE", {"DE"}),
        ("Indianapolis, IN", {"US"}),
        ("San Francisco, CA | Seattle, WA", {"US"}),
        ("London, UK; New York, NY", {"GB", "US"}),
        ("Pune, IN", {"IN"}),
        ("Somewhere, ZZ", set()),
    ],
)
def test_location_codes(text: str, expected: set[str]) -> None:
    assert geo.location_codes(text) == expected


def test_fast_first_place_matches_the_slow_reference_on_every_name() -> None:
    names = [text for _, _, _, text in geo._PLACES]
    for text in names + [n.upper() for n in names] + [f"Office in {n}, hybrid" for n in names]:
        assert geo._first_place(text) == geo._first_place_slow(text), text


def test_country_names() -> None:
    assert geo.country_name("in") == "India"
    assert geo.country_name("AX") == "Åland Islands"
    assert geo.country_name("ZZ") == "ZZ"
    assert name("") == ""
