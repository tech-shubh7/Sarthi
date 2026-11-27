"""Synthetic profiles exercise the same funnel used for live matches."""

from typing import Any

import pytest

from sarthi.jobs.geo import seniority_of
from sarthi.matching.decide import Decision
from sarthi.matching.funnel import evaluate
from sarthi.matching.prefilter import prefilter
from sarthi.matching.rules import check_rules
from sarthi.sources.boards import JobBoardSource
from sarthi.sources.schedule import query_for
from tests.matching.conftest import NOW, make_job, make_profile
from tests.matching.labelled import CASES, Case
from tests.matching.world import PROFILES


@pytest.mark.parametrize(
    ("preferences", "posting", "blocker"),
    [
        pytest.param({"home_country": "IN"}, {}, None, id="india-local-hybrid"),
        pytest.param(
            {"home_country": "GB", "countries": ["DE"]},
            {"country_codes": ["DE"], "visa_sponsorship": "no"},
            "No visa sponsorship",
            id="uk-relocation-no-sponsor",
        ),
        pytest.param(
            {"home_country": "FR", "countries": ["DE"], "authorized_countries": ["DE"]},
            {"country_codes": ["DE"], "visa_sponsorship": "no"},
            None,
            id="france-authorized-germany",
        ),
        pytest.param(
            {"home_country": "US", "remote": "only"},
            {"remote_type": "remote", "country_codes": [], "remote_countries": ["US", "CA"]},
            None,
            id="us-remote-regional",
        ),
        pytest.param(
            {"home_country": "AU", "remote": "only"},
            {"remote_type": "remote", "country_codes": [], "remote_countries": ["US", "CA"]},
            "Remote only for people",
            id="australia-remote-wrong-region",
        ),
        pytest.param(
            {"home_country": "AU", "remote": "only"},
            {"remote_type": "remote", "country_codes": [], "location_text": "Worldwide"},
            None,
            id="australia-remote-worldwide",
        ),
        pytest.param(
            {"home_country": "CA", "years_experience": 0},
            {"country_codes": ["CA"], "title": "Senior Backend Engineer"},
            "Senior level role",
            id="canada-junior-vs-senior",
        ),
        pytest.param(
            {"home_country": "SG", "exclude_companies": ["fictional employer"]},
            {"country_codes": ["SG"], "company": "Fictional Employer Asia"},
            "Excluded company",
            id="singapore-employer-exclusion",
        ),
        pytest.param(
            {"home_country": "GB", "min_salary": {"minimum": 70000, "currency": "GBP"}},
            {"country_codes": ["GB"], "salary": {"minimum": 55000, "maximum": 65000, "currency": "GBP"}},
            "below your 70,000 minimum",
            id="uk-disclosed-maximum-below-floor",
        ),
        pytest.param(
            {"home_country": "GB", "min_salary": {"minimum": 70000, "currency": "GBP"}},
            {"country_codes": ["GB"], "salary": {"minimum": 65000, "currency": "GBP"}},
            None,
            id="uk-open-ended-compensation",
        ),
        pytest.param(
            {"home_country": "GB", "min_salary": {"minimum": 70000, "currency": "GBP"}},
            {"country_codes": ["GB"], "salary": {"maximum": 65000, "currency": "USD"}},
            None,
            id="no-implied-exchange-rate",
        ),
    ],
)
async def test_candidate_profiles_follow_the_full_match_funnel(
    preferences: dict[str, Any], posting: dict[str, Any], blocker: str | None
) -> None:
    profile = make_profile(**preferences)
    job = make_job(**posting)
    rules = check_rules(job, profile)
    result = await evaluate(job, profile, NOW)
    assert rules.eligible is (blocker is None), rules.blockers
    if blocker:
        assert result.decision is Decision.HIDE
        assert blocker in (result.outcome.hidden_because or "")
        assert not result.ai_used
    else:
        assert result.decision is not Decision.HIDE, result.outcome


# ---- job seekers around the world (tests/matching/world.py) and hand-labelled postings (labelled.py) ----

# Where each person searches, and which boards the job-board search adds for that country.
EXPECTED_SEARCH: dict[str, tuple[tuple[str, ...], bool, set[str]]] = {
    "in-ai-engineer": (("IN",), False, {"linkedin", "indeed", "naukri"}),
    "us-data-scientist": (("US",), False, {"linkedin", "indeed"}),
    "gb-product-designer": (("GB",), False, {"linkedin", "indeed"}),
    "de-marketing-manager": (("DE",), False, {"linkedin", "indeed"}),
    "ca-accountant": (("CA",), False, {"linkedin", "indeed"}),
    "au-registered-nurse": (("AU",), False, {"linkedin", "indeed"}),
    "sg-account-executive": (("SG",), False, {"linkedin", "indeed"}),
    "ae-mechanical-engineer": (("AE", "QA", "SA"), False, {"linkedin", "indeed", "bayt"}),
    "nl-teacher": (("NL",), False, {"linkedin", "indeed"}),
    "br-customer-support": (("BR",), False, {"linkedin", "indeed"}),
    "remote-frontend-engineer": (("KE",), True, {"linkedin"}),  # Indeed has no Kenya site
    "in-data-analyst-fresher": (("IN",), False, {"linkedin", "indeed", "naukri"}),
    "de-mechanical-engineer": (("DE",), False, {"linkedin", "indeed"}),
    "gb-staff-nurse": (("GB",), False, {"linkedin", "indeed"}),
}


def test_every_world_profile_has_an_expected_search() -> None:
    assert set(EXPECTED_SEARCH) == set(PROFILES)
    assert len({p.preferences.home_country for p in PROFILES.values()}) >= 11


@pytest.mark.parametrize("key", sorted(PROFILES))
def test_each_person_searches_their_own_countries_and_boards(key: str) -> None:
    countries, remote, boards = EXPECTED_SEARCH[key]
    query = query_for(PROFILES[key], "boards", 24)
    assert query is not None
    assert (query.countries, query.remote) == (countries, remote)
    assert query.roles == PROFILES[key].preferences.roles
    for country in countries:
        assert set(JobBoardSource()._boards_for(country)) == boards


def _verdict(decision: Decision) -> str:
    return {Decision.HIDE: "no", Decision.DIGEST: "possible", Decision.ALERT: "strong"}[decision]


@pytest.mark.parametrize("case", [c for c in CASES if not c.ai_only], ids=lambda c: c.id)
async def test_labelled_postings_shown_or_hidden_as_a_recruiter_would(case: Case) -> None:
    result = await evaluate(case.job(), PROFILES[case.profile], NOW)
    shown = result.decision is not Decision.HIDE
    assert shown is (case.label != "no"), (case.why, result.outcome, result.reasons)


async def test_labelled_set_agreement_without_ai() -> None:
    """The rule layer alone, over every case (the AI-only ones included): measured, and held."""
    verdicts = [_verdict((await evaluate(c.job(), PROFILES[c.profile], NOW)).decision) for c in CASES]
    shown_right = sum((v == "no") == (c.label == "no") for v, c in zip(verdicts, CASES, strict=True))
    exact = sum(v == c.label for v, c in zip(verdicts, CASES, strict=True))
    assert len(CASES) >= 30
    assert shown_right / len(CASES) >= 0.95
    assert exact / len(CASES) >= 0.9


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("Staff Nurse", None),  # the entry grade on an NHS ward
        ("Staff Accountant", None),
        ("Kitchen Staff", None),
        ("Senior Staff Nurse", "senior"),
        ("Staff Engineer", "lead"),
        ("Staff AI Engineer", "lead"),
        ("Software Engineer, Staff", "lead"),
        ("Staff Data Scientist", "lead"),
        ("Staff Product Designer", "lead"),
    ],
)
def test_staff_is_a_level_only_in_tech_titles(title: str, level: str | None) -> None:
    assert seniority_of(title) == level


def test_accounts_executive_is_bookkeeping_not_sales() -> None:
    sales = PROFILES["sg-account-executive"]
    bookkeeping = make_job(
        title="Accounts Executive", country_codes=["SG"], description="Full set of accounts."
    )
    assert not check_rules(bookkeeping, sales).eligible
    assert check_rules(make_job(title="Account Executive", country_codes=["SG"]), sales).eligible
    accountant = PROFILES["ca-accountant"]
    assert check_rules(make_job(title="Accounts Payable Clerk", country_codes=["CA"]), accountant).eligible


@pytest.mark.parametrize(
    ("profile", "title"),
    [
        ("br-customer-support", "Atendente Suporte ao Cliente"),
        ("br-customer-support", "Analista de Atendimento ao Cliente"),
        ("br-customer-support", "Customer Support"),
        ("nl-teacher", "Leerkracht groep 5"),
        ("nl-teacher", "Docent Engels"),
        ("de-marketing-manager", "Leiter Marketing (m/w/d)"),
    ],
)
def test_titles_in_other_languages_and_without_the_job_noun_match(profile: str, title: str) -> None:
    result = prefilter(make_job(title=title), PROFILES[profile], NOW)
    assert result.title_share >= 0.5, result.reasons


def test_one_shared_word_is_still_only_a_partial_match() -> None:
    ml = make_profile(roles=["Machine Learning Engineer"])
    assert prefilter(make_job(title="ML Ops Specialist"), ml, NOW).title_share == 0.5
