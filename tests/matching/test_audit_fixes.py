"""Mistakes found by a manual audit of 557 real matches (2026-11-28), each pinned so it stays fixed."""

import pytest

from sarthi.ai.scoring import AiScore, MatchAnswers, MustHave, combine
from sarthi.jobs.geo import detect_terms
from sarthi.jobs.models import JobPosting
from sarthi.jobs.requirements import min_years_in_title, min_years_required
from sarthi.matching.decide import Decision
from sarthi.matching.funnel import UNREAD_SCORE, evaluate
from sarthi.matching.rules import check_rules
from sarthi.profile.models import Profile
from tests.matching.conftest import NOW, make_job, make_profile


class SaysPerfect:
    """An AI that calls every job a perfect fit: what it said about empty postings."""

    available = True

    def __init__(self) -> None:
        self.calls = 0

    async def score(self, job: JobPosting, profile: Profile) -> AiScore | None:
        self.calls += 1
        return AiScore(100, 0.9, ("Strong match",))


@pytest.mark.parametrize("text", ["", "Apply now.", "Backend Engineer at a fintech in Bengaluru. " * 3])
async def test_a_posting_sarthi_could_not_read_is_never_scored_from_its_title(text: str) -> None:
    ai = SaysPerfect()
    result = await evaluate(make_job(description=text), make_profile(years_experience=1), NOW, ai=ai)
    assert ai.calls == 0  # the AI would only be judging a title
    assert result.score == UNREAD_SCORE and result.decision is Decision.DIGEST
    assert result.outcome.rule == "unread"
    assert "could not read" in result.reasons[0]


async def test_identical_unread_jobs_get_the_same_place() -> None:
    """Empty "Backend Developer" postings were 70 at one company and hidden at 44 at another."""
    ai = SaysPerfect()
    profile = make_profile(years_experience=1)
    a = await evaluate(make_job(description="", company="Agent Alliance"), profile, NOW, ai=ai)
    b = await evaluate(
        make_job(description="", company="Apps For Bharath", country_codes=["IN"]), profile, NOW, ai=ai
    )
    assert (a.score, a.decision) == (b.score, b.decision) == (UNREAD_SCORE, Decision.DIGEST)


async def test_a_job_kept_for_its_skills_not_its_title_is_never_near_perfect() -> None:
    job = make_job(
        title="MERN Stack Developer", description="Python, Go and AWS services, React screens. " * 8
    )
    result = await evaluate(job, make_profile(years_experience=1), NOW, ai=SaysPerfect())
    assert result.score <= 74


def _answers(**overrides: object) -> MatchAnswers:
    data: dict[str, object] = {
        "eligible": 1, "role_fit": 1, "skills_fit": 1, "seniority_fit": 1, "overall_fit": 1, "confidence": 1,
        "must_haves": [MustHave(need="Python", shown=True), MustHave(need="REST APIs", shown=True)],
    }  # fmt: skip
    return MatchAnswers.model_validate(data | overrides)


def test_the_most_important_must_have_missing_is_never_an_alert() -> None:
    shown = [MustHave(need=n, shown=True) for n in ("Python", "REST APIs", "PostgreSQL", "Docker", "Git")]
    top_missing = [MustHave(need="BigCommerce", shown=False), *shown]
    assert combine(_answers(must_haves=top_missing)) <= 70
    later_missing = [*shown, MustHave(need="Kafka", shown=False)]
    assert combine(_answers(must_haves=later_missing)) > 80


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("Hiring an IT Developer with 23 years of experience in React.js, Node.js", 2),  # "2-3", dash lost
        ("Full Stack Engineer (24 Years Experience)", 2),
        ("Minimum 15 years of full time education. Minimum 7.5 years of experience in Python.", 7),
        ("Experience: 3-5 Years", 3),
        ("15+ years of experience leading platform teams", 15),  # a real senior ask stays
        ("12 years of experience in Java", 12),
    ],
)
def test_years_asked(text: str, years: int) -> None:
    assert min_years_required(text) == years


def test_a_title_with_a_lost_dash() -> None:
    assert min_years_in_title("Full Stack Engineer (24 Years Experience)") == 2


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("We build Contract Lifecycle Management software. Employment type: Full-time.", "full_time"),
        ("Write unit, integration and contract tests for our services.", None),
        ("Benefits: flexible schedule, health insurance.", None),
        ("This is a 6-month contract role with a possible extension.", "contract"),
        ("Job type: Part-time, 20 hours per week.", "part_time"),
        ("Job type: Full-time, Part-time", "full_time"),  # an Indeed list: full-time is offered
    ],
)
def test_the_kind_of_job_comes_from_its_terms_not_any_word(text: str, kind: str | None) -> None:
    assert detect_terms("Software Engineer", text).get("employment_type") == kind


@pytest.mark.parametrize("title", ["Software Engineer | $50 - $100/hr", "Gen AI Engineering | $50 per hr"])
def test_an_hourly_rate_is_not_an_hr_job(title: str) -> None:
    assert check_rules(make_job(title=title), make_profile(roles=["Software Engineer"])).eligible
    assert not check_rules(make_job(title="HR Executive"), make_profile(roles=["Software Engineer"])).eligible


def test_a_title_still_names_the_kind() -> None:
    assert (
        detect_terms("Contract Python Developer", "Employment type: Full-time")["employment_type"]
        == "contract"
    )
