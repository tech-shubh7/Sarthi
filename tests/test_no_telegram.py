"""Telegram is optional: with no bot (or no paired chat) every pipeline step runs and the dashboard has it all."""

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from sarthi.app import App, WebActions, build
from sarthi.chat.questions import ASKING_KEY
from sarthi.config import Settings
from sarthi.db.chat import JobStatus
from sarthi.db.documents import DocumentRecord, DocumentStore
from sarthi.db.inbox import InboxQueries
from sarthi.db.matches import MatchRecord
from sarthi.db.notices import KEEP, NoticeStore, plain
from sarthi.db.tasks import TaskRecord
from sarthi.jobs.forms import FormField
from sarthi.matching.handler import match_version
from sarthi.profile.store import profile_path, save_profile
from sarthi.queue import names
from tests.conftest import FakeClock
from tests.matching.conftest import make_job, make_profile
from tests.test_app import SilentApi

OWNER = 111


async def _app(tmp_path: Path, clock: FakeClock, **extra: Any) -> App:
    settings = Settings(_env_file=None, data_dir=tmp_path, llm_fast_models=[], llm_quality_models=[], **extra)
    sarthi = build(settings, clock=clock)
    save_profile(profile_path(tmp_path), make_profile())
    return sarthi


@pytest.fixture
async def sarthi(tmp_path: Path, clock: FakeClock) -> AsyncIterator[App]:
    app = await _app(tmp_path, clock)  # no bot at all: the dashboard only
    try:
        yield app
    finally:
        await app.bot.api.aclose()
        for client in app.ai_clients:
            await client.aclose()
        app.conn.close()


def _job(sarthi: App, n: int = 1, score: int = 90) -> str:
    job_id = sarthi.bot.jobs.save(
        make_job(url=f"https://jobs.lever.co/acme/{n}", title=f"AI Engineer {n}", company="Acme Labs")
    ).job_id
    version = match_version(make_profile())
    sarthi.bot.matches.save(MatchRecord(job_id, version, score, "alert", "strong", None, (), (), True))
    return job_id


def _claimed(sarthi: App, kind: str) -> TaskRecord:
    task = sarthi.queue.claim([kind])
    assert task is not None, f"no {kind} task was queued"
    sarthi.queue.complete(task)
    return task


def _kinds(sarthi: App) -> list[str]:
    return [n.kind for n in NoticeStore(sarthi.conn).recent()]


async def test_a_dashboard_apply_goes_all_the_way_without_telegram(sarthi: App) -> None:
    assert sarthi.telegram is False
    sarthi.bot.chats.set_state("telegram.owner_chat_id", str(OWNER))  # paired once, the bot is gone now
    web = WebActions(sarthi)
    job_id = _job(sarthi)
    assert web.choose(job_id, "apply")
    _claimed(sarthi, names.DOCUMENTS)

    # documents -> form check, though no chat gets the files
    (sarthi.settings.data_dir / "documents").mkdir()
    for name in ("r.pdf", "l.pdf"):
        (sarthi.settings.data_dir / "documents" / name).write_bytes(b"%PDF-1.4 fictional")
    DocumentStore(sarthi.conn).save(DocumentRecord(job_id, "v", "documents/r.pdf", "documents/l.pdf"))
    await sarthi.bot.handle_documents_ready(TaskRecord(1, names.DOCUMENTS_READY, job_id, {"apply": True}, 1))
    _claimed(sarthi, names.FORM_CHECK)

    # the form asks two things only the user knows: they wait on the dashboard
    sarthi.bot.questions.answers.set_pending(  # type: ignore[union-attr]
        job_id,
        [
            FormField("visa", "Do you need a visa?", "select", True, ("Yes", "No")),
            FormField("start", "When can you start?", "date", True),
        ],
    )
    await sarthi.bot.handle_ask_questions(TaskRecord(2, names.ASK_QUESTIONS, job_id, {}, 1))
    groups = InboxQueries(sarthi.conn).questions()
    assert [(g.job_id, [q.key for q in g.questions]) for g in groups] == [(job_id, ["visa", "start"])]
    assert sarthi.queue.claim([names.APPLY]) is None  # nothing is filled before the answers
    assert sarthi.bot.chats.get_state(ASKING_KEY) is None  # nobody asked in a chat

    # a wrong answer is explained and kept back; the right ones continue the application
    assert web.answer_questions(job_id, {"visa": "Maybe", "start": ""}) == {
        "visa": "Please pick one of the listed answers."
    }
    assert web.answer_questions(job_id, {"visa": "no", "start": "2 weeks"}) == {}
    assert InboxQueries(sarthi.conn).questions() == []
    assert sarthi.bot.questions.answers.saved()["Do you need a visa?"] == "No"  # type: ignore[union-attr]
    _claimed(sarthi, names.APPLY)

    # the filled form waits for Submit on the dashboard (while its page is open)
    sarthi.applier.engine.can_submit = lambda job, code: code == "ABC234"  # type: ignore[method-assign]
    preview = {"code": "ABC234", "screenshot": "screenshots/x.png", "filled": [["Name", "Asha"]]}
    await sarthi.bot.handle_apply_preview(TaskRecord(3, names.APPLY_PREVIEW, job_id, preview, 1))
    assert web.pending()[job_id].screenshot == "screenshots/x.png"
    counts = InboxQueries(sarthi.conn).counts(sarthi.clock() - timedelta(days=7))
    assert (counts.forms, counts.questions, counts.strong) == (1, 0, 0)  # the strong match was answered
    assert web.submit(job_id)
    assert _claimed(sarthi, names.SUBMIT).payload == {"code": "ABC234"}

    result = {"outcome": "submitted", "detail": "Sent."}
    await sarthi.bot.handle_apply_result(TaskRecord(4, names.APPLY_RESULT, job_id, result, 1))
    assert sarthi.bot.chats.job_status(job_id) is JobStatus.APPLIED
    assert _kinds(sarthi) == ["result", "preview", "questions", "documents"]


async def test_news_without_a_chat_is_kept_for_the_dashboard(sarthi: App) -> None:
    job_id = _job(sarthi)
    followup = {"job": job_id, "draft": "Hello <team>,\nany news?", "days": 9}
    await sarthi.bot.handle_followup_notice(TaskRecord(1, names.FOLLOWUP_NOTICE, f"{job_id}:1", followup, 1))
    reply = {"job": job_id, "kind": "interview", "subject": "Next steps", "summary": "Call on Monday"}
    await sarthi.bot.handle_reply_notice(TaskRecord(2, names.REPLY_NOTICE, "m1", reply, 1))
    await sarthi.bot.handle_source_notice(
        TaskRecord(3, names.SOURCE_NOTICE, "s", {"source": "boards:x", "streak": 3}, 1)
    )
    await sarthi.bot.handle_digest(TaskRecord(4, names.DIGEST, "d", {}, 1))  # nothing to send, no waiting
    reply_n, followup_n = NoticeStore(sarthi.conn).recent()[1:]
    assert (
        followup_n.kind == "followup" and "Hello <team>,\nany news?" in followup_n.text
    )  # the draft, as text
    assert followup_n.title == "AI Engineer 1" and followup_n.company == "Acme Labs"
    assert reply_n.kind == "reply" and "Call on Monday" in reply_n.text


async def test_auto_apply_waits_its_veto_on_the_dashboard_and_can_be_cancelled(sarthi: App) -> None:
    web = WebActions(sarthi)
    assert web.set_auto_line("85") is None
    first, second = _job(sarthi, 1), _job(sarthi, 2)
    for job_id in (first, second):
        await sarthi.bot.handle_alert(TaskRecord(1, names.ALERT, job_id, {}, 1))
    waiting = InboxQueries(sarthi.conn).auto_waiting()
    assert {w.job_id for w in waiting} == {first, second}
    assert waiting[0].score == 90
    assert "auto_apply" in _kinds(sarthi)

    assert web.cancel_auto(first)
    assert web.apply_now(second)
    assert InboxQueries(sarthi.conn).auto_waiting() == []
    assert not web.apply_now(first)  # cancelled: nothing applies by itself
    await sarthi.bot.handle_auto_apply(TaskRecord(2, names.AUTO_APPLY, first, {}, 1))
    assert sarthi.bot.chats.job_status(first) is None
    assert sarthi.bot.chats.job_status(second) is JobStatus.APPLY
    assert _claimed(sarthi, names.DOCUMENTS).key == second


async def test_questions_can_be_declined_on_the_dashboard(sarthi: App) -> None:
    web = WebActions(sarthi)
    job_id = _job(sarthi)
    sarthi.bot.questions.answers.set_pending(job_id, [FormField("q", "Why us?", "textarea", True)])  # type: ignore[union-attr]
    assert web.decline_questions(job_id)
    assert InboxQueries(sarthi.conn).questions() == []
    assert sarthi.bot.chats.job_status(job_id) is JobStatus.SKIPPED
    assert not web.decline_questions("missing")


async def test_finish_and_site_logins_work_without_a_chat(
    sarthi: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[str, int | None, str]] = []

    async def finish(chat_id: int | None, job_id: str) -> None:
        opened.append(("finish", chat_id, job_id))

    async def log_in(chat_id: int | None, site: str) -> None:
        opened.append(("login", chat_id, site))

    monkeypatch.setattr(sarthi, "_finish", finish)
    monkeypatch.setattr(sarthi, "_log_in", log_in)
    web = WebActions(sarthi)
    job_id = _job(sarthi)
    assert web.finish(job_id)
    assert not web.site_login("naukri")  # one browser window at a time
    await sarthi._login  # type: ignore[misc]
    assert web.site_login("naukri")
    await sarthi._login  # type: ignore[misc]
    assert not web.site_login("myspace")
    assert opened == [("finish", None, job_id), ("login", None, "naukri")]
    assert ("naukri", "Naukri") in web.login_sites()
    assert web.screen_link() is None


async def test_browser_messages_reach_the_dashboard_and_a_paired_chat(
    tmp_path: Path, clock: FakeClock
) -> None:
    sarthi = await _app(tmp_path, clock, telegram_bot_token=SecretStr("1:x"))
    api = SilentApi()
    sarthi.bot.api = api  # type: ignore[assignment]
    try:
        await sarthi._say(None, "Log in to Naukri there.")
        assert api.sent == []
        sarthi.bot.chats.set_state("telegram.owner_chat_id", str(OWNER))
        await sarthi._say(sarthi.bot.chat(), "✅ Logged in to Naukri.")
        assert api.sent == ["✅ Logged in to Naukri."]
        assert [n.text for n in NoticeStore(sarthi.conn).recent()] == [
            "✅ Logged in to Naukri.",
            "Log in to Naukri there.",
        ]
    finally:
        for client in sarthi.ai_clients:
            await client.aclose()
        sarthi.conn.close()


async def test_with_a_paired_chat_telegram_still_gets_everything(tmp_path: Path, clock: FakeClock) -> None:
    sarthi = await _app(tmp_path, clock, telegram_bot_token=SecretStr("1:x"))
    api = SilentApi()
    sarthi.bot.api = api  # type: ignore[assignment]
    try:
        sarthi.bot.questions.api = api  # type: ignore[union-attr, assignment]
        sarthi.bot.chats.set_state("telegram.owner_chat_id", str(OWNER))
        job_id = _job(sarthi)
        sarthi.bot.questions.answers.set_pending(  # type: ignore[union-attr]
            job_id, [FormField("visa", "Do you need a visa?", "select", True, ("Yes", "No"))]
        )
        await sarthi.bot.handle_ask_questions(TaskRecord(1, names.ASK_QUESTIONS, job_id, {}, 1))
        assert "Do you need a visa?" in api.sent[-1]
        assert sarthi.bot.chats.get_state(ASKING_KEY) is not None
        # answered on the dashboard instead: the chat's question is done, the application goes on
        assert WebActions(sarthi).answer_questions(job_id, {"visa": "Yes"}) == {}
        assert sarthi.bot.chats.get_state(ASKING_KEY) is None
        _claimed(sarthi, names.APPLY)
        await sarthi.bot.handle_reply_notice(
            TaskRecord(2, names.REPLY_NOTICE, "m", {"job": job_id, "kind": "offer"}, 1)
        )
        assert "Offer" in api.sent[-1]
        assert "reply" in _kinds(sarthi)  # the dashboard has it too
    finally:
        for client in sarthi.ai_clients:
            await client.aclose()
        sarthi.conn.close()


def test_notices_are_plain_text_and_only_the_newest_are_kept(conn: Any, clock: FakeClock) -> None:
    store = NoticeStore(conn, clock=clock)
    assert plain("<b>Applied</b> &amp; <i>done</i>") == "Applied & done"
    store.add("x", "")  # nothing to keep
    for n in range(KEEP + 5):
        store.add("x", f"n{n}", job_id="not-a-stored-job")
    kept = store.recent(KEEP)
    assert len(kept) == KEEP and kept[0].text == f"n{KEEP + 4}" and kept[0].job_id is None
