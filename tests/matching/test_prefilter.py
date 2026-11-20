from datetime import timedelta

from sarthi.matching.decide import Thresholds
from sarthi.matching.prefilter import KEYWORD_POINTS, TITLE_POINTS, prefilter, tokens
from tests.matching.conftest import NOW, make_job, make_profile


def test_tokens_keep_tech_names() -> None:
    assert tokens("C++, C#, Node.js and APIs") == ["c++", "c#", "node.js", "and", "api"]


def test_exact_title_and_all_keywords_score_high() -> None:
    result = prefilter(make_job(), make_profile(), NOW)
    assert result.score >= TITLE_POINTS + 0.7 * KEYWORD_POINTS
    assert "Title matches your target role (backend engineer)" in result.reasons
    assert any(r.startswith("Mentions python, go, aws") for r in result.reasons)


def test_unrelated_title_scores_low() -> None:
    result = prefilter(
        make_job(title="Sales Manager", description="Quota, CRM, field sales."), make_profile(), NOW
    )
    assert result.score < Thresholds().hide_below / 2  # far below the hide line
    assert "Title does not match your target roles" in result.reasons


def test_words_out_of_order_get_partial_title_credit() -> None:
    exact = prefilter(make_job(title="Backend Engineer"), make_profile(), NOW).score
    shuffled = prefilter(make_job(title="Engineer, Backend Platform"), make_profile(), NOW).score
    assert 0 < shuffled < exact


def test_seniority_fit_and_mismatch() -> None:
    profile = make_profile(seniority=["mid"])
    assert any("Seniority fits" in r for r in prefilter(make_job(seniority="mid"), profile, NOW).reasons)
    principal = prefilter(make_job(seniority="principal"), profile, NOW)
    assert any("Seniority mismatch" in r for r in principal.reasons)
    assert principal.score < prefilter(make_job(seniority="mid"), profile, NOW).score


def test_salary_only_compared_in_the_same_currency_and_period() -> None:
    floor = {"currency": "INR", "period": "year", "minimum": 1_500_000}
    profile = make_profile(min_salary=floor)
    low = make_job(salary={"currency": "INR", "period": "year", "minimum": 800_000, "maximum": 1_000_000})
    high = make_job(salary={"currency": "INR", "period": "year", "maximum": 2_500_000})
    dollars = make_job(salary={"currency": "USD", "period": "year", "maximum": 50_000})
    assert any("below your 1,500,000 floor" in r for r in prefilter(low, profile, NOW).reasons)
    assert any("Pays up to INR 2,500,000" in r for r in prefilter(high, profile, NOW).reasons)
    assert not any("Pays" in r for r in prefilter(dollars, profile, NOW).reasons)


def test_older_jobs_score_lower() -> None:
    fresh = prefilter(make_job(), make_profile(), NOW).score
    stale = prefilter(make_job(posted_at=NOW - timedelta(days=90)), make_profile(), NOW).score
    assert stale < fresh


def test_resume_terms_count_when_no_keywords_hit() -> None:
    profile = make_profile(keywords=[]).model_copy(
        update={"skills": (), "resume_text": "kafka kafka kubernetes terraform"}
    )
    job = make_job(title="Platform Engineer", description="We run Kafka on Kubernetes with Terraform.")
    assert any("Shares 3 terms with your resume" in r for r in prefilter(job, profile, NOW).reasons)


def test_seniority_is_inferred_from_years_when_not_set() -> None:
    three_years = make_profile(years_experience=3)
    staff = prefilter(make_job(title="Staff Software Engineer", seniority="lead"), three_years, NOW)
    mid = prefilter(make_job(title="Software Engineer", seniority="mid"), three_years, NOW)
    assert any("Seniority mismatch" in r for r in staff.reasons)
    assert staff.score < mid.score
    senior = prefilter(make_job(seniority="senior"), make_profile(years_experience=4), NOW)
    assert not any("mismatch" in r for r in senior.reasons)  # 3-6 years fits mid or senior
    unknown = prefilter(make_job(seniority="lead"), make_profile(years_experience=None), NOW)
    assert not any("Seniority" in r for r in unknown.reasons)
    veteran = prefilter(make_job(seniority="lead"), make_profile(years_experience=12), NOW)
    assert any("Seniority fits" in r for r in veteran.reasons)
