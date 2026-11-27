"""Synthetic profiles exercise the same funnel used for live matches."""

from typing import Any

import pytest

from sarthi.matching.decide import Decision
from sarthi.matching.funnel import evaluate
from sarthi.matching.rules import check_rules
from tests.matching.conftest import NOW, make_job, make_profile


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
