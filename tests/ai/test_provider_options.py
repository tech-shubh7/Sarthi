"""DeepSeek is asked not to think (thinking is billed as output); other providers get the plain request."""

import json
import sqlite3

import httpx
import pytest
from pydantic import SecretStr

from sarthi.ai.answers import interpret_list
from sarthi.ai.client import AiClient
from sarthi.ai.providers import Endpoint
from sarthi.db.ai_store import AiStore
from tests.conftest import FakeClock


@pytest.mark.parametrize(
    ("endpoint", "thinking"),
    [
        (
            Endpoint("deepseek", "deepseek-flash", "https://api.deepseek.example/v1", SecretStr("k")),
            {"type": "disabled"},
        ),
        (Endpoint("groq", "gpt-oss-20b", "https://api.groq.example/v1", SecretStr("k")), None),
    ],
)
async def test_thinking_is_turned_off_only_for_deepseek(
    conn: sqlite3.Connection, clock: FakeClock, endpoint: Endpoint, thinking: object
) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        reply = {"keep": ["Backend Engineer"], "add": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    ai = AiClient(
        [endpoint], AiStore(conn, clock=clock), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    assert await interpret_list(ai, "Which roles?", ["Backend Engineer"], "keep it") == ["Backend Engineer"]
    assert bodies[0].get("thinking") == thinking
    assert bodies[0]["response_format"] == {"type": "json_object"}
