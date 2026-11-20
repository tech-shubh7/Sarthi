from typing import Any

import pytest

from sarthi.matching.rules import LOCATION_FIT_POINTS, LOCATION_UNSURE_POINTS, check_rules
from tests.matching.conftest import make_job, make_profile

# An Indian user: works in India, would also move to the UK (needs a visa there), remote is fine.
USER: dict[str, Any] = {"countries": ["GB"]}


@pytest.mark.parametrize(
    ("job", "eligible", "message"),
    [
        ({}, True, None),
        ({"country_codes": ["DE"]}, False, "Located in Germany, outside your countries"),
        ({"country_codes": ["GB"], "visa_sponsorship": "no"}, False, "No visa sponsorship"),
        ({"country_codes": ["GB"], "visa_sponsorship": "yes"}, True, "Offers visa sponsorship"),
        ({"country_codes": ["GB"]}, True, "Visa sponsorship not mentioned"),
        ({"country_codes": [], "remote_type": "unknown"}, True, "Location country not stated"),
        ({"remote_type": "remote", "country_codes": []}, True, "Remote, open worldwide"),
        ({"remote_type": "remote", "remote_countries": ["IN"]}, True, "open to people in India"),
        (
            {"remote_type": "remote", "remote_countries": ["US"]},
            False,
            "Remote only for people in United States",
        ),
        ({"employment_type": "internship"}, False, "Internship role"),
        ({"employment_type": "unknown"}, True, None),
        ({"description": "Needs 8+ years of Go experience."}, False, "Asks for 8+ years"),
        ({"description": "Needs 5+ years of Go experience."}, True, "Asks for 5+ years; you have 3"),
        (
            {"description": "Pay a refundable security deposit to confirm your joining."},
            False,
            "Looks like a scam",
        ),
    ],
)
def test_rules_for_an_indian_user(job: dict[str, Any], eligible: bool, message: str | None) -> None:
    result = check_rules(make_job(**job), make_profile(**USER))
    assert result.eligible is eligible
    lines = result.blockers + result.notes
    if message:
        assert any(message in line for line in lines), lines
    if not eligible:
        assert result.location_points == 0


def test_visa_free_country_needs_no_sponsorship_note() -> None:
    result = check_rules(
        make_job(country_codes=["AE"], visa_sponsorship="no"),
        make_profile(countries=["AE"], authorized_countries=["AE"]),
    )
    assert result.eligible
    assert result.notes == ()
    assert result.location_points == LOCATION_FIT_POINTS


def test_unknown_country_scores_less_than_a_known_fit() -> None:
    result = check_rules(make_job(country_codes=[]), make_profile())
    assert result.location_points == LOCATION_UNSURE_POINTS


@pytest.mark.parametrize(
    ("remote", "job", "eligible"),
    [
        ("only", {"remote_type": "hybrid"}, False),
        ("only", {"remote_type": "onsite"}, False),
        ("only", {"remote_type": "unknown"}, True),  # unknown never hides
        ("only", {"remote_type": "remote", "country_codes": []}, True),
        ("no", {"remote_type": "remote", "country_codes": []}, False),
        ("no", {"remote_type": "onsite"}, True),
    ],
)
def test_remote_preference(remote: str, job: dict[str, Any], eligible: bool) -> None:
    assert check_rules(make_job(**job), make_profile(remote=remote)).eligible is eligible


def test_exclusions() -> None:
    profile = make_profile(exclude_companies=["razorpay"], exclude_keywords=["blockchain"])
    assert check_rules(make_job(company="Razorpay Software Pvt Ltd"), profile).blockers == (
        "Excluded company: razorpay",
    )
    other = make_job(company="Acme", description="Blockchain payments team.")
    assert check_rules(other, profile).blockers == ("Contains excluded keyword: blockchain",)
    assert check_rules(make_job(company="Acme", description="Onchain-free payments."), profile).eligible


def test_medium_scam_risk_is_a_note_not_a_blocker() -> None:
    result = check_rules(
        make_job(description="Earn ₹5,000 per day from home. Urgent hiring!"), make_profile()
    )
    assert result.eligible
    assert result.scam_risk == "medium"
    assert any("Possible scam" in note for note in result.notes)
