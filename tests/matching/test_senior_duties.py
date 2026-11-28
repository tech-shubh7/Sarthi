"""A plain title with a senior's duties is out of reach early in a career (no title level, no years asked)."""

import pytest

from sarthi.jobs.requirements import senior_duties
from sarthi.matching.rules import check_rules
from tests.matching.conftest import make_job, make_profile

MEESHO_LIKE = """Responsibilities:
Align the engineering roadmap with business outcomes.
Translate company goals and customer needs into a technical roadmap.
Design and evolve core platforms for APIs, data, and AI that are scalable and observable.
Embed AI as a first-class concern: request orchestration, evaluation harnesses, guardrails.
Mentor: Raise the bar through reviews, design reviews, and hands-on pairing; be a model others emulate."""


def test_senior_duties_are_read_from_the_responsibilities() -> None:
    found = senior_duties(MEESHO_LIKE)
    assert len(found) >= 2 and any("roadmap" in f for f in found) and "mentor" in found


@pytest.mark.parametrize(
    "text",
    [
        "Junior Backend Engineer. You will mentor interns and join design reviews.",  # early-career posting
        "Build REST APIs in Python. You will be mentored by senior engineers.",  # being mentored
        "Backend Engineer, 0-2 years. Own the roadmap for one service and mentor new joiners.",
        "Build Python services with FastAPI, Docker and PostgreSQL. Write tests.",
    ],
)
def test_ordinary_or_early_career_postings_are_not_senior(text: str) -> None:
    assert len(senior_duties(text)) < 2


def test_a_senior_job_with_a_plain_title_is_ruled_out_for_one_year() -> None:
    job = make_job(title="Backend Engineer", description=MEESHO_LIKE)
    result = check_rules(job, make_profile(years_experience=1))
    assert not result.eligible
    assert any("Senior-level duties" in b for b in result.blockers)
    assert check_rules(job, make_profile(years_experience=6)).eligible


def test_a_plain_junior_friendly_job_stays_eligible() -> None:
    job = make_job(title="Backend Engineer", description="Build Python APIs. A senior will mentor you.")
    assert check_rules(job, make_profile(years_experience=1)).eligible
