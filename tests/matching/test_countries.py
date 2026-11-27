"""Sarthi for users outside India: places, location rules, citizenship and language gates (fictional data)."""

from __future__ import annotations

from typing import Any

import pytest

from sarthi.jobs import geo
from sarthi.jobs.models import RemoteType
from sarthi.jobs.normalize import complete
from sarthi.matching.rules import check_rules
from sarthi.profile.models import Profile

from .conftest import make_job, make_profile

EVERYONE = {"US", "GB", "DE", "CA", "AU", "SG", "AE", "NL", "IN"}


@pytest.mark.parametrize(
    ("location", "codes"),
    [
        ("NYC", {"US"}),
        ("Brooklyn, New York", {"US"}),
        ("Cambridge, MA", {"US"}),
        ("Cambridge, Massachusetts, United States", {"US"}),
        ("Melbourne, FL", {"US"}),
        ("Albuquerque, New Mexico", {"US"}),
        ("London, ON", {"CA"}),
        ("London, Ontario, Canada", {"CA"}),
        ("Mississauga, ON", {"CA"}),
        ("Montréal, QC", {"CA"}),
        ("Sydney, New South Wales, Australia", {"AU"}),
        ("Perth, WA", {"AU"}),
        ("Parramatta, NSW", {"AU"}),
        ("München, Bayern", {"DE"}),
        ("Düsseldorf", {"DE"}),
        ("Utrecht", {"NL"}),
        ("Den Haag", {"NL"}),
        ("Chennai, TN", {"IN"}),
        ("Bombay", {"IN"}),
        ("Gurugram, Haryana", {"IN"}),
        ("Toronto, CA", {"CA"}),
        ("Palo Alto, CA", {"US"}),
        ("Remote, DE", {"DE"}),
        ("London, UK; Berlin, Germany", {"GB", "DE"}),
        ("Berlin and London", {"DE", "GB"}),
        ("Remote - DACH", {"DE", "AT", "CH"}),
        ("Remote - ANZ", {"AU", "NZ"}),
    ],
)
def test_places_around_the_world(location: str, codes: set[str]) -> None:
    assert geo.location_codes(location) == codes


def _passes(location: str, remote: bool) -> set[str]:
    job = complete(
        make_job(
            country_codes=[],
            location_text=location,
            remote_type=RemoteType.REMOTE if remote else RemoteType.ONSITE,
            description="Build data pipelines in Python.",
        )
    )
    return {home for home in EVERYONE if check_rules(job, make_profile(home_country=home)).eligible}


@pytest.mark.parametrize(
    ("location", "remote", "who"),
    [
        ("New York, NY", False, {"US"}),
        ("Greater London", False, {"GB"}),
        ("Berlin", False, {"DE"}),
        ("Toronto, ON", False, {"CA"}),
        ("Sydney NSW", False, {"AU"}),
        ("Singapore", False, {"SG"}),
        ("Dubai - United Arab Emirates", False, {"AE"}),
        ("Amsterdam, North Holland, Netherlands", False, {"NL"}),
        ("Bengaluru, Karnataka", False, {"IN"}),
        ("Remote - US only", True, {"US"}),
        ("Remote - UK", True, {"GB"}),
        ("Remote - EU", True, {"DE", "NL"}),
        ("Remote - APAC", True, {"AU", "SG", "IN"}),
        ("Remote - Americas", True, {"US", "CA"}),
        ("Remote - Worldwide", True, EVERYONE),
        ("Anywhere in the World", True, EVERYONE),
    ],
)
def test_who_a_job_is_for(location: str, remote: bool, who: set[str]) -> None:
    assert _passes(location, remote) == who


def test_a_remote_anywhere_user_living_outside_the_tables_gets_worldwide_jobs_only() -> None:
    profile = make_profile(home_country="BR", remote="only")
    worldwide = complete(make_job(country_codes=[], location_text="Remote", remote_type="remote"))
    us_only = complete(make_job(country_codes=[], location_text="Remote (US)", remote_type="remote"))
    assert check_rules(worldwide, profile).eligible
    assert not check_rules(us_only, profile).eligible


# ---- citizenship, residency and clearance -----------------------------------------------------------------


def _rules(description: str, profile: Profile, **job: Any) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    result = check_rules(make_job(description=description, **job), profile)
    return result.eligible, result.blockers, result.notes


def test_citizenship_required_elsewhere_rules_the_job_out() -> None:
    london = make_profile(home_country="GB", countries=["US"])  # would move, but is no US citizen
    eligible, blockers, _ = _rules("Applicants must be US citizens.", london, country_codes=["US"])
    assert not eligible
    assert blockers == ("Only for citizens or residents of United States (“must be US citizen”)",)
    us = make_profile(home_country="US")
    assert _rules("Applicants must be US citizens.", us, country_codes=["US"])[0]


def test_clearance_is_for_the_jobs_own_country() -> None:
    text = "Requires an active Secret clearance."
    mover = make_profile(home_country="IN", countries=["US"])
    assert not _rules(text, mover, country_codes=["US"])[0]
    resident = make_profile(home_country="IN", countries=["US"], authorized_countries=["US"])
    assert _rules(text, resident, country_codes=["US"])[0]
    eligible, _, notes = _rules(text, make_profile(home_country="IN"), country_codes=[])
    assert eligible  # the job's country is unknown: a note, not a guess
    assert any("Secret clearance" in note for note in notes)


@pytest.mark.parametrize(
    ("text", "country"),
    [
        ("Saudi nationals only.", "SA"),
        ("UAE National Only", "AE"),
        ("This role is only for Qatari nationals.", "QA"),
        ("Kuwaiti nationals are required for this position.", "KW"),
    ],
)
def test_gulf_nationals_only_is_a_citizenship_rule(text: str, country: str) -> None:
    dubai = make_profile(home_country="AE", countries=["SA", "QA", "KW"])
    eligible, blockers, _ = _rules(text, dubai, country_codes=[country])
    assert eligible is (country == "AE")
    if country != "AE":
        assert blockers and "Only for citizens or residents" in blockers[0]
    assert _rules("UAE nationals are preferred.", dubai, country_codes=["AE"])[0]  # a wish, not a rule


@pytest.mark.parametrize(
    ("listed", "language"),
    [
        ("Português", "Portuguese"),
        ("Español (C1)", "Spanish"),
        ("हिन्दी", "Hindi"),
        ("தமிழ்", "Tamil"),
        ("日本語", "Japanese"),
        ("العربية", "Arabic"),
        ("Русский B2", "Russian"),
        ("Bahasa Indonesia", "Indonesian"),
    ],
)
def test_languages_written_in_their_own_name_count(listed: str, language: str) -> None:
    # Before: a Brazilian listing "Português" was ruled out of every job asking for Portuguese.
    profile = make_profile(home_country="BR", languages=[listed])
    eligible, blockers, _ = _rules(f"Fluent {language} required.", profile, country_codes=["BR"])
    assert eligible, blockers


@pytest.mark.parametrize(
    "description",
    [
        "Join our citizen developer team building low-code tools.",
        "A permanent role with a pension. Visa sponsorship available.",
        "No security clearance required.",
        "Build APIs in Python.",
    ],
)
def test_silence_and_lookalikes_pass(description: str) -> None:
    assert _rules(description, make_profile(home_country="GB"), country_codes=["GB"])[0]


def test_sponsorship_offered_is_noted_for_someone_who_needs_it() -> None:
    job = complete(make_job(description="We sponsor visas for the right person.", country_codes=["NL"]))
    result = check_rules(job, make_profile(home_country="IN", countries=["NL"]))
    eligible, notes = result.eligible, result.notes
    assert eligible
    assert "Offers visa sponsorship" in notes


# ---- languages ---------------------------------------------------------------------------------------------


def test_a_required_language_the_user_lacks_rules_the_job_out() -> None:
    berlin = make_profile(home_country="DE", languages=["French"])
    eligible, blockers, _ = _rules("Fluent German required. Python.", berlin, country_codes=["DE"])
    assert not eligible
    assert blockers == ("Requires German (“Fluent German”), not one of your languages",)


def test_until_languages_are_listed_a_required_language_is_only_a_note() -> None:
    berlin = make_profile(home_country="DE")  # languages not filled in yet
    eligible, blockers, notes = _rules("Fluent German required. Python.", berlin, country_codes=["DE"])
    assert eligible and not blockers
    assert any("add the languages you speak in Settings" in note for note in notes)


def test_a_higher_level_than_the_users_is_a_note() -> None:
    berlin = make_profile(home_country="DE", languages=["German B1"])
    eligible, _, notes = _rules("Fluent German required.", berlin, country_codes=["DE"])
    assert eligible
    assert "Asks for German at C1; you listed B1" in notes
    native = make_profile(home_country="DE", languages=["German"])  # no level given: any level
    assert _rules("Fluent German required.", native, country_codes=["DE"])[0]


@pytest.mark.parametrize(
    "description",
    [
        "Our team speaks English; German is a plus.",
        "Wir suchen einen Data Engineer. Arbeitssprache ist Englisch, Deutsch ist von Vorteil.",
        "Fluent English required.",
        "Our office dog is a German shepherd.",
    ],
)
def test_wishes_the_ads_own_language_and_english_pass(description: str) -> None:
    assert _rules(description, make_profile(home_country="DE"), country_codes=["DE"])[0]


def test_languages_left_unset_keep_the_profile_version() -> None:
    plain = make_profile()
    assert plain.version == make_profile(languages=[]).version
    assert plain.version != make_profile(languages=["Dutch"]).version
