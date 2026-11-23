import json
import sqlite3
from typing import Any

import httpx
import pytest

from sarthi.ai.client import AiClient
from sarthi.ai.providers import Endpoint
from sarthi.ai.scoring import LlmMatchScorer, MatchAnswers, combine, coverage, gap_lines
from sarthi.db.ai_store import AiStore
from tests.conftest import FakeClock
from tests.matching.conftest import make_job, make_profile

LOCAL = Endpoint("ollama", "llama", "http://localhost:11434/v1", None)


def answers(**overrides: Any) -> MatchAnswers:
    data: dict[str, Any] = {
        "eligible": 1,
        "role_fit": 1,
        "skills_fit": 1,
        "seniority_fit": 1,
        "overall_fit": 1,
        "confidence": 0.9,
    }
    return MatchAnswers.model_validate(data | overrides)


def test_combine_uses_fixed_weights() -> None:
    assert combine(answers()) == 100
    assert combine(answers(seniority_fit=0.5)) == 90
    assert combine(answers(role_fit=0, skills_fit=0, seniority_fit=0, overall_fit=0)) == 0


def need(name: str, shown: bool) -> dict[str, Any]:
    return {"need": name, "shown": shown}


def test_the_score_follows_the_must_haves_the_resume_shows() -> None:
    three_of_four = [need("Python", True), need("FastAPI", True), need("RAG", True), need("Kafka", False)]
    assert coverage(answers(must_haves=three_of_four)) == 0.75
    assert combine(answers(must_haves=three_of_four)) == 91
    # The model's skills_fit counts only when it listed no must-haves.
    assert combine(answers(must_haves=three_of_four, skills_fit=0)) == 91


def test_a_wrong_kind_of_role_is_capped_however_well_the_keywords_read() -> None:
    # Seen live: an AI (LLM apps) engineer was shown "ML Engineer, fraud models" jobs at 70-90%.
    ml_training = answers(
        role_fit=0.3, overall_fit=0.6, must_haves=[need("Python", True), need("PyTorch", False)]
    )
    assert combine(ml_training) <= 45
    thin = answers(must_haves=[need("Java", False), need("Spring", False), need("Python", True)])
    assert combine(thin) <= 55


def test_missing_must_haves_become_the_gap_line_when_the_model_names_none() -> None:
    answer = answers(must_haves=[need("Python", True), need("Kafka", False), need("Spark", False)])
    assert gap_lines(answer) == ("Not shown on your resume: Kafka, Spark",)
    assert gap_lines(answers(gaps=["No Kubernetes"])) == ("No Kubernetes",)
    assert gap_lines(answers()) == ()


def test_probably_ineligible_is_scaled_down_hard() -> None:
    assert combine(answers(eligible=0.4)) == 30


def test_answers_are_validated() -> None:
    with pytest.raises(ValueError, match="less than or equal to 1"):
        answers(skills_fit=1.5)
    # one line too many from a model is cut, not a lost answer
    assert answers(strengths=["one reason", "two reason", "three reason"]).strengths == [
        "one reason",
        "two reason",
    ]


def scorer(conn: sqlite3.Connection, clock: FakeClock, handler: Any) -> LlmMatchScorer:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LlmMatchScorer(
        AiClient([LOCAL], AiStore(conn, clock=clock), requests_per_minute=6000, http=http, clock=clock)
    )


async def test_scorer_sends_a_scrubbed_profile_and_returns_the_verdict(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["messages"][1]["content"])
        content = answers(
            skills_fit=0.5, strengths=["Strong Python match"], gaps=["Kafka not on your resume"]
        ).model_dump_json()
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    profile = make_profile(looking_for="Fintech backend roles").model_copy(
        update={"resume_text": "me@example.com +91 98765 43210\nBuilt payment APIs in Python."}
    )
    verdict = await scorer(conn, clock, handler).score(make_job(), profile)
    assert verdict is not None
    assert (verdict.score, verdict.confidence) == (82, 0.9)  # 82.5 rounds half to even
    assert verdict.reasons == ("Strong Python match", "✗ Kafka not on your resume")  # a gap is marked
    prompt = sent[0]
    assert "me@example.com" not in prompt
    assert "98765" not in prompt
    assert "Built payment APIs in Python." in prompt
    assert "Looking for: Fintech backend roles" in prompt
    assert "Title: Backend Engineer\nCompany: Razorpay" in prompt


async def test_scorer_returns_none_when_the_ai_cannot_answer(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    failing = scorer(conn, clock, lambda request: httpx.Response(503))
    assert await failing.score(make_job(), make_profile()) is None
    unconfigured = LlmMatchScorer(AiClient([], AiStore(conn, clock=clock)))
    assert await unconfigured.score(make_job(), make_profile()) is None


def test_a_vague_posting_is_never_a_near_perfect_match() -> None:
    # Seen live: "Knowledge of programming languages", location "to be confirmed", scored 94.
    assert combine(answers(confidence=0.4)) == 70
    assert combine(answers(confidence=0.9)) == 100
