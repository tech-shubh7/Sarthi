import json
import sqlite3

import httpx

from sarthi.ai.client import AiClient
from sarthi.ai.providers import Endpoint
from sarthi.ai.resume import read_resume
from sarthi.db.ai_store import AiStore
from tests.conftest import FakeClock

LOCAL = Endpoint("ollama", "qwen", "http://localhost:11434/v1", None)


async def test_resume_is_scrubbed_and_read(conn: sqlite3.Connection, clock: FakeClock) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["messages"][1]["content"])
        content = json.dumps(
            {"skills": ["Python", "Go"], "years_experience": 4, "roles": ["Backend Engineer"]}
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = AiClient(
        [LOCAL], AiStore(conn, clock=clock), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    facts = await read_resume(
        client, "Asha Rao | asha@example.com | +91 98765 43210\nPython and Go, 4 years."
    )
    assert facts is not None
    assert (facts.skills, facts.years_experience, facts.roles) == (["Python", "Go"], 4, ["Backend Engineer"])
    assert "asha@example.com" not in sent[0]
    assert "98765" not in sent[0]
    assert "<resume>" in sent[0]
    await client.aclose()


async def test_no_model_or_no_text_means_no_suggestions(conn: sqlite3.Connection, clock: FakeClock) -> None:
    assert await read_resume(AiClient([], AiStore(conn, clock=clock)), "Python") is None
    failing = AiClient(
        [LOCAL],
        AiStore(conn, clock=clock),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    assert await read_resume(failing, "Python and Go") is None
    assert await read_resume(failing, "   ") is None
    await failing.aclose()
