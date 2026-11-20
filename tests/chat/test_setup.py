import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sarthi.ai.client import AiClient
from sarthi.ai.providers import Endpoint
from sarthi.ai.resume import ResumeFacts
from sarthi.chat import setup as setup_flow
from sarthi.chat.setup import STATE_KEY, SetupFlow, Step
from sarthi.chat.telegram.bot import TelegramBot
from sarthi.db.ai_store import AiStore
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.jobs.models import SalaryPeriod
from sarthi.profile.models import Preferences, Profile, RemotePreference
from sarthi.profile.store import load_profile, save_profile
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


def picker(api: FakeFiles) -> list[str]:
    """Button labels on the last message."""
    markup = api.sent[-1][2]
    assert markup is not None
    return [button["text"] for row in markup["inline_keyboard"] for button in row]


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
    assert picker(api) == ["✅ Backend Engineer", "✔️ Done"]
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
    assert picker(api) == ["✅ Backend Engineer", "✔️ Done"]  # current values become the picker
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
    assert "remove ML engineer, add Data Engineer" in api.texts()[-1]
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


async def test_small_talk_and_side_questions_are_never_saved_as_answers(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, ai_reads: None
) -> None:
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await answer(bot, "hi")
    assert api.texts()[-2] == "Hi! 👋"
    assert "job titles" in api.texts()[-1]  # the question again
    await answer(bot, "why do you need my roles?")
    assert "I search LinkedIn" in api.texts()[-2]
    await answer(bot, "thanks")
    assert api.texts()[-2] == "You're welcome! 🙂"
    state = flow._state()
    assert state is not None
    assert "roles" not in state["draft"]  # nothing was saved from the small talk


async def test_a_correction_mid_setup_updates_that_answer_and_skips_its_question(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, ai_reads: None
) -> None:
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await answer(bot, "ok", "India")
    await answer(bot, "actually change my salary to 15 LPA")  # while being asked about countries
    assert api.texts()[-2] == "✓ Minimum salary: <b>INR 1,500,000 / year</b> (updated)"
    assert "other countries" in api.texts()[-1]
    await answer(bot, "UK", "none")
    await bot.handle_update(tap(OWNER, "setup:remote:ok"))
    await answer(bot, "ok", "ok")
    assert "Minimum salary: INR 1,500,000 / year" in api.texts()[-1]  # not asked again: straight to summary
    assert not any("Minimum salary</b> you'd accept" in t for t in api.texts()[-3:])


async def test_after_setup_profile_changes_by_message_with_undo(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, ai_reads: None
) -> None:
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await answer(bot, "ok", "India", "none", "none")
    await bot.handle_update(tap(OWNER, "setup:remote:ok"))
    await answer(bot, "ok", "ok", "skip")
    await bot.handle_update(tap(OWNER, "setup:save"))

    await answer(bot, "please add UAE to my countries")
    reply, markup = api.texts()[-1], api.sent[-1][2]
    assert reply == "✓ Other countries: <b>United Arab Emirates</b> (saved)"
    assert markup == {"inline_keyboard": [[{"text": "↩️ Undo", "callback_data": "profile:undo"}]]}
    profile = load_profile(flow.profile_file)
    assert profile is not None
    assert profile.preferences.countries == {"AE"}
    await answer(bot, "actually I want remote only")
    assert "Remote: <b>Remote only</b> (saved)" in api.texts()[-1]
    await bot.handle_update(tap(OWNER, "profile:undo"))
    restored = load_profile(flow.profile_file)
    assert restored is not None
    assert restored.preferences.remote is RemotePreference.OK
    await bot.handle_update(tap(OWNER, "profile:undo"))
    assert api.texts()[-1] == "Nothing to undo."


async def test_outside_setup_unclear_messages_get_gentle_help(bot: TelegramBot, api: FakeFiles) -> None:
    await answer(bot, "hello")
    assert "watching new jobs" in api.texts()[-1]
    await answer(bot, "add Python to my skills")
    assert "no profile yet" in api.texts()[-1]
    await answer(bot, "what can you do?")
    assert "/today" in api.texts()[-1]


async def test_setup_never_suggests_junk_saved_by_older_versions(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow
) -> None:
    save_profile(
        flow.profile_file,
        Profile(
            preferences=Preferences(roles=("Backend Engineer", "Expect Fintech"), home_country="IN"),
            skills=("Use skills",),
        ),
    )
    await answer(bot, "/setup", "skip")
    assert "Expect Fintech" not in api.texts()[-1]
    assert picker(api) == ["✅ Backend Engineer", "✔️ Done"]  # junk role dropped
    await answer(bot, "ok", "India", "none", "none")
    await bot.handle_update(tap(OWNER, "setup:remote:ok"))
    await answer(bot, "1")
    assert "Use skills" not in api.texts()[-1]  # the skills question shows no junk suggestion


async def test_take_skills_from_resume_reads_the_resume_again(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []

    async def fake_read(client: AiClient, text: str, today: object = None) -> ResumeFacts:
        reads.append(text)
        return ResumeFacts(
            skills=["Python", "FastAPI", "Use skills"], years_experience=1.0, roles=["Backend Engineer"]
        )

    monkeypatch.setattr(setup_flow, "read_resume", fake_read)
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await answer(bot, "ok", "India", "none", "none")
    await bot.handle_update(tap(OWNER, "setup:remote:ok"))
    await answer(bot, "ok", "use skills take skills from resume")
    assert len(reads) == 2  # read again for the skills
    assert api.texts()[-2] == "✓ Skills: <b>Python, FastAPI</b>"  # and junk from the AI is dropped


async def test_take_from_resume_without_a_resume_says_so(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow
) -> None:
    await answer(bot, "/setup", "skip", "Backend Engineer", "India", "none", "none")
    await bot.handle_update(tap(OWNER, "setup:remote:ok"))
    await answer(bot, "1", "take skills from my resume")
    assert "I don't have your resume yet" in api.texts()[-1]


async def test_tap_to_remove_picker(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_read(client: AiClient, text: str, today: object = None) -> ResumeFacts:
        return ResumeFacts(
            skills=["Python"],
            years_experience=1.0,
            roles=["Backend Engineer", "Machine Learning Engineer", "AI Engineer"],
        )

    monkeypatch.setattr(setup_flow, "read_resume", fake_read)
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    assert picker(api) == ["✅ Backend Engineer", "✅ Machine Learning Engineer", "✅ AI Engineer", "✔️ Done"]
    await bot.handle_update(tap(OWNER, "setup:pick:1"))
    edited = api.edits[-1][2]
    assert edited is not None
    assert [b["text"] for row in edited["inline_keyboard"] for b in row][1] == "❌ Machine Learning Engineer"
    await bot.handle_update(tap(OWNER, "setup:pick:2"))
    await bot.handle_update(tap(OWNER, "setup:pick:2"))  # tapped again: back in
    await bot.handle_update(tap(OWNER, "setup:pick:99"))  # out of range: ignored
    await bot.handle_update(tap(OWNER, "setup:pick:done"))
    assert api.texts()[-2] == "✓ Roles: <b>Backend Engineer, AI Engineer</b>"


async def test_picker_keeps_at_least_one_role(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, ai_reads: None
) -> None:
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await bot.handle_update(tap(OWNER, "setup:pick:0"))
    await bot.handle_update(tap(OWNER, "setup:pick:done"))
    assert "Keep at least one job title" in api.texts()[-1]
    assert flow.step is Step.ROLES


async def test_typing_while_the_picker_is_open_edits_what_is_still_selected(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_read(client: AiClient, text: str, today: object = None) -> ResumeFacts:
        return ResumeFacts(
            skills=["Python"],
            years_experience=1.0,
            roles=["Backend Engineer", "Machine Learning Engineer", "AI Engineer"],
        )

    monkeypatch.setattr(setup_flow, "read_resume", fake_read)
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await bot.handle_update(tap(OWNER, "setup:pick:2"))  # AI Engineer off
    await answer(bot, "ok, add Data Engineer")
    assert api.texts()[-2] == "✓ Roles: <b>Backend Engineer, Machine Learning Engineer, Data Engineer</b>"


async def test_remove_ml_engineer_stays_removed(
    bot: TelegramBot, api: FakeFiles, flow: SetupFlow, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_read(client: AiClient, text: str, today: object = None) -> ResumeFacts:
        return ResumeFacts(
            skills=["Python"],
            years_experience=1.0,
            roles=["Backend Engineer", "Machine Learning Engineer", "AI Engineer"],
        )

    async def ai_drops_it(client: AiClient, question: str, current: list[str], answer_text: str) -> list[str]:
        return [item for item in current if item != "Machine Learning Engineer"]  # the AI understood "ML"

    monkeypatch.setattr(setup_flow, "read_resume", fake_read)
    monkeypatch.setattr(setup_flow, "interpret_list", ai_drops_it)
    flow.ai = AiClient([LOCAL_AI], flow.ai._store)
    api.files["f1"] = RESUME
    await answer(bot, "/setup")
    await bot.handle_update(document())
    await answer(bot, "remove ML engineer")
    assert api.texts()[-2] == "✓ Roles: <b>Backend Engineer, AI Engineer</b>"  # no rule puts it back
