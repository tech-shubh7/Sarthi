from typing import Any

import pytest

from sarthi.ai.scoring import AiScore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.jobs.models import JobPosting
from sarthi.matching.decide import Decision, Thresholds, decide
from sarthi.matching.funnel import evaluate
from sarthi.matching.handler import ALERT_TASK, Matcher
from sarthi.matching.rules import RulesResult
from sarthi.profile.models import Profile
from sarthi.queue.tasks import DeferError, PermanentError, TaskQueue
from tests.conftest import FakeClock
from tests.matching.conftest import NOW, make_job, make_profile


class FakeAi:
    def __init__(self, verdict: AiScore | None) -> None:
        self.verdict = verdict
        self.calls = 0

    async def score(self, job: JobPosting, profile: Profile) -> AiScore | None:
        self.calls += 1
        return self.verdict


@pytest.mark.parametrize(
    ("rules", "score", "confidence", "decision", "rule"),
    [
        (
            RulesResult(eligible=False, blockers=("Located in Germany",)),
            90,
            None,
            Decision.HIDE,
            "not_eligible",
        ),
        (RulesResult(eligible=True), 20, None, Decision.HIDE, "weak"),
        (RulesResult(eligible=True, scam_risk="medium"), 95, None, Decision.DIGEST, "possible_scam"),
        (RulesResult(eligible=True), 95, 0.3, Decision.DIGEST, "unsure"),
        (RulesResult(eligible=True), 75, 0.9, Decision.ALERT, "strong"),
        (RulesResult(eligible=True), 50, None, Decision.DIGEST, "fair"),
    ],
)
def test_decision_table(
    rules: RulesResult, score: int, confidence: float | None, decision: Decision, rule: str
) -> None:
    outcome = decide(rules, score, Thresholds(), confidence)
    assert (outcome.decision, outcome.rule) == (decision, rule)


async def test_strong_free_match_alerts_without_ai() -> None:
    result = await evaluate(make_job(), make_profile(), NOW)
    assert result.decision is Decision.ALERT
    assert not result.ai_used
    assert result.reasons


async def test_ineligible_jobs_never_reach_the_ai() -> None:
    ai = FakeAi(AiScore(99, 1.0))
    result = await evaluate(make_job(country_codes=["DE"]), make_profile(), NOW, ai=ai)
    assert (result.decision, result.score, ai.calls) == (Decision.HIDE, 0, 0)


async def test_weak_free_score_never_reaches_the_ai() -> None:
    ai = FakeAi(AiScore(99, 1.0))
    job = make_job(title="Sales Manager", description="Field sales.")
    result = await evaluate(job, make_profile(), NOW, ai=ai)
    assert (result.decision, ai.calls) == (Decision.HIDE, 0)


async def test_ai_score_blends_in_and_leads_the_reasons() -> None:
    ai = FakeAi(AiScore(20, 0.9, ("Needs Rust, which your resume does not show",)))
    result = await evaluate(make_job(), make_profile(), NOW, ai=ai)
    assert result.ai_used
    assert result.reasons[0] == "Needs Rust, which your resume does not show"
    assert result.decision is not Decision.ALERT


async def test_unavailable_ai_falls_back_to_the_free_score() -> None:
    result = await evaluate(make_job(), make_profile(), NOW, ai=FakeAi(None))
    assert (result.ai_used, result.decision) == (False, Decision.ALERT)


@pytest.fixture
def matcher(job_store: JobStore, conn: Any, queue: TaskQueue, clock: FakeClock) -> Any:
    def build(profile: Profile | None) -> Matcher:
        return Matcher(job_store, MatchStore(conn, clock=clock), queue, lambda: profile, clock=clock)

    return build


async def test_handler_stores_the_match_and_queues_an_alert(matcher: Any, job_store: JobStore) -> None:
    saved = job_store.save(make_job())
    m: Matcher = matcher(make_profile())
    await m.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    record = m.matches.get(saved.job_id)
    assert record is not None
    assert record.decision == "alert"
    assert record.profile_version == make_profile().version
    alert = m.queue.claim([ALERT_TASK])
    assert alert is not None
    assert alert.key == saved.job_id
    assert m.matches.counts() == {"alert": 1}


async def test_hidden_job_is_stored_with_its_reason_and_not_alerted(
    matcher: Any, job_store: JobStore
) -> None:
    saved = job_store.save(make_job(country_codes=["DE"]))
    m: Matcher = matcher(make_profile())
    await m.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    record = m.matches.get(saved.job_id)
    assert record is not None
    assert (record.decision, record.hidden_because) == ("hide", "Located in Germany, outside your countries")
    assert m.queue.claim([ALERT_TASK]) is None


async def test_handler_waits_for_a_profile_and_fails_for_missing_jobs(matcher: Any) -> None:
    with pytest.raises(DeferError):
        await matcher(None).handle(TaskRecord(1, "match", "any", {}, 1))
    with pytest.raises(PermanentError):
        await matcher(make_profile()).handle(TaskRecord(1, "match", "missing", {}, 1))


async def test_handler_judges_the_full_text_not_the_snippet(matcher: Any, job_store: JobStore) -> None:
    snippet_only = make_job(description="Build payment APIs.")
    saved = job_store.save(
        snippet_only, full_text="Build payment APIs. Requires 9+ years of backend experience."
    )
    m: Matcher = matcher(make_profile())
    await m.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    record = m.matches.get(saved.job_id)
    assert record is not None
    assert (record.decision, record.hidden_because) == ("hide", "Asks for 9+ years of experience")


async def test_background_scoring_waits_while_the_user_is_chatting(
    job_store: JobStore, conn: Any, queue: TaskQueue, clock: FakeClock
) -> None:
    saved = job_store.save(make_job())
    busy = {"chat": True}
    matcher = Matcher(
        job_store,
        MatchStore(conn, clock=clock),
        queue,
        make_profile,
        ai=FakeAi(AiScore(80, 0.9)),
        ai_paused=lambda: busy["chat"],
        clock=clock,
    )
    with pytest.raises(DeferError):
        await matcher.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    busy["chat"] = False
    await matcher.handle(TaskRecord(1, "match", saved.job_id, {}, 1))
    assert matcher.matches.get(saved.job_id) is not None
