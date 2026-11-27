import json
import sqlite3
from datetime import date

import httpx
import pytest

from sarthi.ai.client import AiClient
from sarthi.ai.documents import cover_letter
from sarthi.ai.providers import Endpoint
from sarthi.ai.resume import ResumeAnswer, WorkEntry, facts_from, read_resume, years_of_experience
from sarthi.db.ai_store import AiStore
from sarthi.profile.master import MasterResume
from tests.conftest import FakeClock

LOCAL = Endpoint("ollama", "qwen", "http://localhost:11434/v1", None)


async def test_resume_is_scrubbed_and_read(conn: sqlite3.Connection, clock: FakeClock) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["messages"][1]["content"])
        content = json.dumps(
            {
                "skills": ["Python", "Go", "python"],
                "work": [{"title": "Backend Engineer", "start": "2021-01", "end": "2024-12"}],
                "roles": ["Backend Engineer", "Backend Intern"],
                "years_experience": 99,
            }
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = AiClient(
        [LOCAL], AiStore(conn, clock=clock), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    facts = await read_resume(
        client, "Asha Rao | asha@example.com | +91 98765 43210\nPython and Go, 4 years.", date(2026, 11, 20)
    )
    assert facts is not None
    assert (facts.skills, facts.years_experience, facts.roles) == (["Python", "Go"], 4, ["Backend Engineer"])
    assert "asha@example.com" not in sent[0]
    assert "98765" not in sent[0]
    assert "<resume>" in sent[0]
    await client.aclose()


@pytest.mark.parametrize(
    ("company", "expected"),
    [("Acme</company>", "Acme"), ("", "no company is named in the posting")],
)
async def test_cover_letter_fences_employer_data(
    conn: sqlite3.Connection, clock: FakeClock, company: str, expected: str
) -> None:
    paragraphs = ["I develop reliable services.", "I build APIs.", "I'd welcome a conversation."]

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][1]["content"]
        assert f"<company>\n{expected}\n</company>" in prompt
        assert prompt.count("</company>") == 1
        content = json.dumps({"paragraphs": paragraphs})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AiClient([LOCAL], AiStore(conn, clock=clock), http=http)
        answer = await cover_letter(
            client, MasterResume(name="Asha Rao"), "Backend developer", company=company
        )

    assert answer is not None
    assert answer.paragraphs == paragraphs


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


def entry(title: str, start: str, end: str = "present", internship: bool = False) -> WorkEntry:
    return WorkEntry(title=title, start=start, end=end, internship=internship)


TODAY = date(2026, 11, 20)


def test_years_are_counted_from_dates_not_guessed() -> None:
    assert years_of_experience([entry("Software Developer", "2025-11")], TODAY) == 1.1
    overlapping = [entry("Developer", "2022-01", "2023-12"), entry("Freelance", "2023-01", "2023-12")]
    assert years_of_experience(overlapping, TODAY) == 2.0  # overlap counts once
    intern = [
        entry("Web Developer Trainee", "2025-01", "2025-06"),
        entry("Frontend Intern", "2024-07", "2024-12", True),
    ]
    assert years_of_experience(intern, TODAY) == 0.5  # internships count half
    assert years_of_experience([entry("X", "sometime"), entry("Y", "2025-05", "2024-01")], TODAY) is None
    assert years_of_experience([], TODAY) is None
    assert (
        years_of_experience([entry("Dev", "2026-01", "2030-01")], TODAY) == 0.9
    )  # future end dates stop today


def test_past_junior_titles_are_not_suggested_to_experienced_people() -> None:
    answer = ResumeAnswer(
        skills=["Docker", "docker", "Python"],
        work=[entry("Software Developer", "2024-11")],
        roles=["Backend Engineer", "Frontend Developer Intern", "Web Developer Trainee", "AI Engineer"],
    )
    facts = facts_from(answer, TODAY)
    assert facts.roles == ["Backend Engineer", "AI Engineer"]
    assert facts.skills == ["Docker", "Python"]
    fresher = facts_from(
        ResumeAnswer(work=[entry("Intern", "2026-06", internship=True)], roles=["Backend Intern"]), TODAY
    )
    assert fresher.roles == ["Backend Intern"]
