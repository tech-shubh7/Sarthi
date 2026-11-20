import sqlite3

import pytest

from sarthi.ai.client import AiClient
from sarthi.ai.intent import Field, IntentAnswer, Kind
from sarthi.chat import understand
from sarthi.chat.understand import Route, route, route_by_rules
from sarthi.db.ai_store import AiStore
from tests.conftest import FakeClock


@pytest.mark.parametrize(
    ("text", "asking", "expected"),
    [
        ("hi", Field.ROLES, Route(Kind.SMALLTALK)),
        ("thanks!", None, Route(Kind.SMALLTALK)),
        ("Backend Engineer, ML Engineer", Field.ROLES, Route(Kind.ANSWER, Field.ROLES)),
        ("actually add UAE to my countries", Field.ROLES, Route(Kind.CORRECTION, Field.COUNTRIES)),
        ("change salary to 15 LPA", Field.SKILLS, Route(Kind.CORRECTION, Field.SALARY)),
        ("i live in Dubai now", Field.YEARS, Route(Kind.CORRECTION, Field.HOME)),
        ("also add ML engineer to roles", Field.SKILLS, Route(Kind.CORRECTION, Field.ROLES)),
        ("why do you need my salary?", Field.SALARY, Route(Kind.QUESTION, Field.SALARY)),
        ("remote only please", Field.REMOTE, Route(Kind.ANSWER, Field.REMOTE)),
        ("2 years", Field.YEARS, Route(Kind.ANSWER, Field.YEARS)),
        ("tell me a joke", Field.ROLES, None),
        ("what is the weather?", Field.ROLES, None),
        ("hello there friend", None, None),
    ],
)
def test_rules(text: str, asking: Field | None, expected: Route | None) -> None:
    assert route_by_rules(text, asking) == expected


async def test_unclear_messages_go_to_the_ai(
    conn: sqlite3.Connection, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def classify(client: AiClient, question: str, message: str) -> IntentAnswer:
        return IntentAnswer(kind=Kind.OFF_TOPIC)

    monkeypatch.setattr(understand, "classify", classify)
    no_ai = AiClient([], AiStore(conn, clock=clock))
    assert await route(no_ai, "tell me a joke", Field.ROLES, "Which roles?") == Route(
        Kind.OFF_TOPIC, Field.ROLES
    )


async def test_without_ai_unclear_answers_stay_answers(conn: sqlite3.Connection, clock: FakeClock) -> None:
    no_ai = AiClient([], AiStore(conn, clock=clock))
    assert await route(no_ai, "give me your best guess", Field.YEARS, "Years?") == Route(
        Kind.ANSWER, Field.YEARS
    )
    assert await route(no_ai, "hello there friend", None, "") == Route(Kind.OFF_TOPIC)


async def test_ai_corrections_without_a_field_use_the_words(
    conn: sqlite3.Connection, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def classify(client: AiClient, question: str, message: str) -> IntentAnswer:
        return IntentAnswer(kind=Kind.CORRECTION)

    monkeypatch.setattr(understand, "classify", classify)
    no_ai = AiClient([], AiStore(conn, clock=clock))
    assert await route(no_ai, "my salary hopes went up, you know", Field.ROLES, "Q") == Route(
        Kind.CORRECTION, Field.SALARY
    )
    assert await route(no_ai, "you know what i mean", Field.ROLES, "Q") == Route(Kind.QUESTION)
