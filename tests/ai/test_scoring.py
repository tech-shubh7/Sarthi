import json
import sqlite3
from typing import Any

import httpx
import pytest

from sarthi.ai.client import AiClient
from sarthi.ai.providers import Endpoint
from sarthi.ai.scoring import LlmMatchScorer, MatchAnswers, combine
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
    assert combine(answers(skills_fit=0)) == 65
    assert combine(answers(role_fit=0, skills_fit=0, seniority_fit=0, overall_fit=0)) == 0


def test_probably_ineligible_is_scaled_down_hard() -> None:
    assert combine(answers(eligible=0.4)) == 30


def test_answers_are_validated() -> None:
    with pytest.raises(ValueError, match="less than or equal to 1"):
        answers(skills_fit=1.5)
    with pytest.raises(ValueError, match="at most 3"):
        answers(reasons=["one reason", "two reason", "three reason", "four reason"])


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
            skills_fit=0.5, reasons=["Strong Python match", "Kafka not on your resume"]
        ).model_dump_json()
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    profile = make_profile(looking_for="Fintech backend roles").model_copy(
        update={"resume_text": "me@example.com +91 98765 43210\nBuilt payment APIs in Python."}
    )
    verdict = await scorer(conn, clock, handler).score(make_job(), profile)
    assert verdict is not None
    assert (verdict.score, verdict.confidence) == (82, 0.9)  # 82.5 rounds half to even
    assert verdict.reasons == ("Strong Python match", "Kafka not on your resume")
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
