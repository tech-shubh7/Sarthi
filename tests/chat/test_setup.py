import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sarthi.ai.client import AiClient
from sarthi.ai.providers import Endpoint
from sarthi.ai.resume import ResumeFacts
from sarthi.chat import setup as setup_flow
from sarthi.chat.setup import STATE_KEY, SetupFlow
from sarthi.chat.telegram.bot import TelegramBot
from sarthi.db.ai_store import AiStore
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.jobs.models import SalaryPeriod
from sarthi.profile.models import RemotePreference
from sarthi.profile.store import load_profile
from sarthi.queue.tasks import PermanentError, TaskQueue
from tests.chat.test_bot import OWNER, FakeApi, message, tap
from tests.conftest import FakeClock
from tests.matching.conftest import make_job

LOCAL_AI = Endpoint("ollama", "qwen", "http://localhost:11434/v1", None)
RESUME = b"Asha Rao\nBackend engineer, 4 years: Python, Go, PostgreSQL, AWS. Built payment APIs for 2M users."


class FakeFiles(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.files: dict[str, bytes | Exception] = {}

    async def download(self, file_id: str, max_bytes: int) -> bytes:
        result = self.files[file_id]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def api() -> FakeFiles:
    return FakeFiles()


@pytest.fixture
def flow(
    api: FakeFiles,
    conn: sqlite3.Connection,
    tmp_path: Path,
    job_store: JobStore,
    queue: TaskQueue,
    clock: FakeClock,
) -> SetupFlow:
    ai = AiClient([], AiStore(conn, clock=clock))
    return SetupFlow(
        api,
        ChatStore(conn, clock=clock),
        ai,
        tmp_path / "profile.json",
        jobs=job_store,
        queue=queue,
        clock=clock,
    )  # type: ignore[arg-type]


@pytest.fixture
def bot(
    api: FakeFiles,
    flow: SetupFlow,
    job_store: JobStore,
    conn: sqlite3.Connection,
    queue: TaskQueue,
    clock: FakeClock,
) -> TelegramBot:
    flow.chats.set_state("telegram.owner_chat_id", str(OWNER))
    return TelegramBot(
        api, flow.chats, job_store, MatchStore(conn, clock=clock), queue, setup=flow, clock=clock
    )  # type: ignore[arg-type]


def document(file_id: str = "f1", size: int = 2000) -> dict[str, Any]:
    return {"message": {"chat": {"id": OWNER}, "document": {"file_id": file_id, "file_size": size}}}


@pytest.fixture
def ai_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_read(client: AiClient, text: str, today: object = None) -> ResumeFacts:
        assert "Asha Rao" in text
        return ResumeFacts(
            skills=["Python", "Go", "PostgreSQL"], years_experience=4, roles=["Backend Engineer"]
        )

    monkeypatch.setattr(setup_flow, "read_resume", fake_read)


async def answer(bot: TelegramBot, *texts: str) -> None:
    for text in texts:
        await bot.handle_update(message(OWNER, text))


async def test_full_setup_saves_a_profile_and_rematches_recent_jobs(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, job_store: JobStore, ai_reads: None
) -> None:
    job_store.save(make_job())
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    assert "send me your <b>resume</b>" in api.texts()[-1]
    await bot.handle_update(document())
    assert "Suggested: <b>Backend Engineer</b>" in api.texts()[-1]
    await answer(bot, "ok", "India", "UK, UAE", "none")
    assert api.sent[-1][2] is not None  # remote question has buttons
    await bot.handle_update(tap(OWNER, "setup:remote:ok"))
    assert "Suggested: <b>4 years</b>" in api.texts()[-1]
    await answer(bot, "ok", "ok", "12 LPA")
    summary = api.texts()[-1]
    for line in (
        "Roles: Backend Engineer",
        "Lives in: India",
        "Would work in: India, United Arab Emirates, United Kingdom",
        "No visa needed in: India",
        "Experience: 4 years",
        "Skills: Python, Go, PostgreSQL",
        "Minimum salary: INR 1,200,000 / year",
        "Resume: saved",
    ):
        assert line in summary
    await bot.handle_update(tap(OWNER, "setup:save"))
    assert "matching 1 recent job" in api.texts()[-1]

    profile = load_profile(flow.profile_file)
    assert profile is not None
    prefs = profile.preferences
    assert (prefs.roles, prefs.home_country, prefs.countries, prefs.remote) == (
        ("Backend Engineer",),
        "IN",
        {"GB", "AE"},
        RemotePreference.OK,
    )
    assert prefs.min_salary is not None
    assert (prefs.min_salary.minimum, prefs.min_salary.period) == (1_200_000, SalaryPeriod.YEAR)
    assert "Asha Rao" in profile.resume_text
    assert flow.chats.get_state(STATE_KEY) is None
    assert flow.queue.claim(["match"]) is not None


async def test_wrong_answers_are_asked_again(bot: TelegramBot, api: FakeFiles, flow: SetupFlow) -> None:
    await answer(bot, "/setup", "skip")
    await answer(bot, "")
    await answer(bot, "Data Analyst", "Atlantis")
    assert "one country" in api.texts()[-1]
    await answer(bot, "India", "Narnia")
    assert "I don't know Narnia" in api.texts()[-1]
    await answer(bot, "none", "none", "maybe")
    assert "tap one of the buttons" in api.texts()[-1]
    await answer(bot, "ok", "many", "3", "SQL, Excel", "lots of money")
    assert "Try e.g. 12 LPA" in api.texts()[-1]
    await answer(bot, "skip", "save please")
    assert "Tap ✅ Save" in api.texts()[-1]
    await bot.handle_update(tap(OWNER, "setup:save"))
    profile = load_profile(flow.profile_file)
    assert profile is not None
    assert profile.preferences.min_salary is None
    assert profile.resume_text == ""


async def test_upload_problems_are_explained(bot: TelegramBot, api: FakeFiles) -> None:
    await bot.handle_update(document())
    assert "send /setup first" in api.texts()[-1]
    await answer(bot, "/setup", "a resume, honestly")
    assert "as a file" in api.texts()[-1]
    await bot.handle_update(document(size=6 * 1024 * 1024))
    assert "over 5 MB" in api.texts()[-1]
    api.files["bad"] = b"\x89PNG\r\n" + bytes(range(256))
    await bot.handle_update(document("bad"))
    assert "PDF, a Word" in api.texts()[-1]
    api.files["gone"] = PermanentError("telegram getFile: file too large or unavailable")
    await bot.handle_update(document("gone"))
    assert "couldn't download" in api.texts()[-1]


async def test_no_ai_means_plain_questions(bot: TelegramBot, api: FakeFiles) -> None:
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    assert "No AI model answered" in api.texts()[-2]
    assert "Suggested" not in api.texts()[-1]


async def test_cancel_profile_and_restart(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, ai_reads: None
) -> None:
    await answer(bot, "/profile")
    assert "no profile yet" in api.texts()[-1]
    await answer(bot, "/setup", "/cancel")
    assert "Setup cancelled" in api.texts()[-1]
    assert not flow.active
    await answer(bot, "/cancel")
    assert "didn't understand" in api.texts()[-1]

    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await answer(bot, "ok", "India", "none", "none")
    await bot.handle_update(tap(OWNER, "setup:remote:only"))
    await answer(bot, "ok", "ok", "skip")
    await bot.handle_update(tap(OWNER, "setup:save"))
    await answer(bot, "/profile")
    assert "Remote: Remote only" in api.texts()[-1]

    await answer(bot, "/setup")
    assert "keep the one you sent before" in api.texts()[-1]
    await answer(bot, "skip")
    assert "Suggested: <b>Backend Engineer</b>" in api.texts()[-1]  # current values become suggestions
    await bot.handle_update(tap(OWNER, "setup:restart"))
    assert "send me your <b>resume</b>" in api.texts()[-1]


async def test_commands_still_work_during_setup(bot: TelegramBot, api: FakeFiles) -> None:
    await answer(bot, "/setup", "/status")
    assert "Sarthi status" in api.texts()[-1]


async def test_editing_the_suggested_roles_in_plain_words(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_read(client: AiClient, text: str, today: object = None) -> ResumeFacts:
        return ResumeFacts(
            skills=["Python", "Docker"],
            years_experience=1.1,
            roles=["Backend Engineer", "Frontend Developer Intern", "AI Engineer"],
        )

    monkeypatch.setattr(setup_flow, "read_resume", fake_read)
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    assert "ok, remove X, add Y" in api.texts()[-1]
    await answer(bot, "remove frontend developer intern, other okay", "India", "none", "none")
    await bot.handle_update(tap(OWNER, "setup:remote:only"))
    await answer(bot, "ok", "ok, add FastAPI", "skip")
    summary = api.texts()[-1]
    assert "Roles: Backend Engineer, AI Engineer" in summary
    assert "Skills: Python, Docker, FastAPI" in summary


async def test_each_answer_is_repeated_back_with_a_change_button(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, ai_reads: None
) -> None:
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await answer(bot, "ok, add AI Engineer")
    _, understood, markup = api.sent[-2]
    assert understood == "✓ Roles: <b>Backend Engineer, AI Engineer</b>"
    assert markup == {"inline_keyboard": [[{"text": "✏️ Change", "callback_data": "setup:back:roles"}]]}
    await answer(bot, "India", "UK")
    assert api.texts()[-2] == "✓ Other countries: <b>United Kingdom</b>"

    # three questions later, fix the roles: the flow comes back to the current question afterwards
    await bot.handle_update(tap(OWNER, "setup:back:roles"))
    assert "job titles" in api.texts()[-1]
    await answer(bot, "Data Engineer")
    assert api.texts()[-2] == "✓ Roles: <b>Data Engineer</b>"
    assert "without a visa" in api.texts()[-1]  # back at the question the user was on
    await answer(bot, "none")
    await bot.handle_update(tap(OWNER, "setup:remote:ok"))
    await answer(bot, "ok", "use these", "skip")
    assert "Roles: Data Engineer" in api.texts()[-1]
    assert "Skills: Python, Go, PostgreSQL" in api.texts()[-1]


async def test_going_forward_or_to_unknown_steps_is_ignored(bot: TelegramBot, api: FakeFiles) -> None:
    await answer(bot, "/setup", "skip")
    sent = len(api.sent)
    for data in ("setup:back:salary", "setup:back:nonsense", "setup:back:resume"):
        await bot.handle_update(tap(OWNER, data))
    assert len(api.sent) == sent


async def test_free_form_edits_go_to_the_ai_and_apply_to_the_current_list(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, ai_reads: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[list[str], str]] = []

    async def fake_interpret(client: AiClient, question: str, current: list[str], answer: str) -> list[str]:
        seen.append((current, answer))
        return [item for item in current if "Backend" not in item] + ["ML Engineer"]

    monkeypatch.setattr(setup_flow, "interpret_list", fake_interpret)
    flow.ai = AiClient([LOCAL_AI], flow.ai._store)
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await answer(bot, "Data Engineer, Backend Engineer")  # a plain list: rules, no AI
    assert seen == []
    await bot.handle_update(tap(OWNER, "setup:back:roles"))
    await answer(bot, "remove the backend one and add ML engineer instead")
    assert seen == [
        (["Data Engineer", "Backend Engineer"], "remove the backend one and add ML engineer instead")
    ]
    assert "Understanding your answer" in api.texts()[-3]
    assert api.texts()[-2] == "✓ Roles: <b>Data Engineer, ML Engineer</b>"


async def test_without_ai_the_rules_handle_edits(bot: TelegramBot, api: FakeFiles, ai_reads: None) -> None:
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await answer(bot, "ok, add Data Engineer")
    assert api.texts()[-2] == "✓ Roles: <b>Backend Engineer, Data Engineer</b>"
    assert not any("Understanding" in text for text in api.texts())
