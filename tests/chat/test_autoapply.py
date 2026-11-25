"""Auto-apply: opt-in line, veto window, cancel, apply now, daily limit, manual taps win."""

import sqlite3

import pytest

from sarthi.chat.autoapply import MIN_LINE, AutoApplySettings
from sarthi.chat.telegram.bot import TelegramBot
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchRecord, MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue import names
from sarthi.queue.tasks import TaskQueue
from tests.chat.test_bot import OWNER, FakeApi, message, pair, tap
from tests.conftest import FakeClock
from tests.matching.conftest import make_job

WAIT_S = 7200.0


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def chats(conn: sqlite3.Connection, clock: FakeClock) -> ChatStore:
    return ChatStore(conn, clock=clock)


def make_bot(
    api: FakeApi,
    chats: ChatStore,
    job_store: JobStore,
    conn: sqlite3.Connection,
    queue: TaskQueue,
    clock: FakeClock,
    line: int | None = 85,
    limit: int | None = None,
) -> TelegramBot:
    settings = AutoApplySettings(line=line, daily_limit=limit, wait_s=WAIT_S)
    matches = MatchStore(conn, clock=clock)
    return TelegramBot(api, chats, job_store, matches, queue, auto_apply=settings, clock=clock)  # type: ignore[arg-type]


@pytest.fixture
def bot(
    api: FakeApi,
    chats: ChatStore,
    job_store: JobStore,
    conn: sqlite3.Connection,
    queue: TaskQueue,
    clock: FakeClock,
) -> TelegramBot:
    bot = make_bot(api, chats, job_store, conn, queue, clock)
    pair(bot, chats, clock)
    return bot


def strong(bot: TelegramBot, score: int, n: int = 1, *, ai_used: bool = True) -> str:
    job_id = bot.jobs.save(
        make_job(url=f"https://boards.greenhouse.io/acme/jobs/{n}", title=f"AI Engineer {n}")
    ).job_id
    bot.matches.save(MatchRecord(job_id, "v", score, "alert", "strong", None, ("Python fits",), (), ai_used))
    return job_id


async def alert(bot: TelegramBot, job_id: str) -> None:
    await bot.handle_alert(TaskRecord(1, names.ALERT, job_id, {}, 1))


async def test_a_match_above_the_line_is_announced_and_waits_for_the_veto_window(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, queue: TaskQueue, clock: FakeClock
) -> None:
    job_id = strong(bot, 90)
    await alert(bot, job_id)
    notice = api.sent[-1]
    assert "I'll apply to" in notice[1] and "2 hours" in notice[1]
    assert notice[2] is not None
    assert [b["callback_data"] for row in notice[2]["inline_keyboard"] for b in row] == [
        f"auto:cancel:{job_id}",
        f"auto:now:{job_id}",
    ]
    assert chats.is_auto(job_id)
    assert queue.claim([names.AUTO_APPLY]) is None  # not before the window ends
    clock.advance(WAIT_S + 1)
    task = queue.claim([names.AUTO_APPLY])
    assert task is not None
    await bot.handle_auto_apply(task)
    assert chats.job_status(job_id) is JobStatus.APPLY
    documents = queue.claim([names.DOCUMENTS])
    assert documents is not None
    assert documents.payload == {"apply": True}


@pytest.mark.parametrize(
    ("score", "ai_used"),
    [(84, True), (95, False)],  # under the line; a keyword score the AI never checked
)
async def test_below_the_line_or_unchecked_stays_a_normal_alert(
    bot: TelegramBot, api: FakeApi, chats: ChatStore, score: int, ai_used: bool
) -> None:
    job_id = strong(bot, score, ai_used=ai_used)
    await alert(bot, job_id)
    assert not chats.is_auto(job_id)
    assert not any("I'll apply to" in t for t in api.texts())


async def test_cancel_in_the_window_stops_it_and_the_card_still_works(
    bot: TelegramBot, chats: ChatStore, queue: TaskQueue, clock: FakeClock
) -> None:
    job_id = strong(bot, 92)
    await alert(bot, job_id)
    await bot.handle_update(tap(OWNER, f"auto:cancel:{job_id}"))
    assert not chats.is_auto(job_id)
    clock.advance(WAIT_S + 1)
    task = queue.claim([names.AUTO_APPLY])
    assert task is not None
    await bot.handle_auto_apply(task)
    assert chats.job_status(job_id) is JobStatus.ALERTED  # nothing started
    assert queue.claim([names.DOCUMENTS]) is None


async def test_apply_now_starts_at_once_and_the_later_task_does_nothing_more(
    bot: TelegramBot, chats: ChatStore, queue: TaskQueue, clock: FakeClock
) -> None:
    job_id = strong(bot, 92)
    await alert(bot, job_id)
    await bot.handle_update(tap(OWNER, f"auto:now:{job_id}"))
    assert chats.job_status(job_id) is JobStatus.APPLY
    assert chats.is_auto(job_id)  # the fill may still be sent without a tap
    first = queue.claim([names.DOCUMENTS])
    assert first is not None
    queue.complete(first)
    clock.advance(WAIT_S + 1)
    task = queue.claim([names.AUTO_APPLY])
    assert task is not None
    await bot.handle_auto_apply(task)
    assert queue.claim([names.DOCUMENTS]) is None  # not twice


async def test_the_users_own_apply_tap_means_the_usual_preview(bot: TelegramBot, chats: ChatStore) -> None:
    job_id = strong(bot, 92)
    await alert(bot, job_id)
    await bot.handle_update(tap(OWNER, f"apply:{job_id}"))
    assert not chats.is_auto(job_id)


async def test_the_daily_limit_holds(
    api: FakeApi,
    chats: ChatStore,
    job_store: JobStore,
    conn: sqlite3.Connection,
    queue: TaskQueue,
    clock: FakeClock,
) -> None:
    bot = make_bot(api, chats, job_store, conn, queue, clock, limit=2)
    pair(bot, chats, clock)
    jobs = [strong(bot, 95, n) for n in range(3)]
    for job_id in jobs:
        await alert(bot, job_id)
    assert [chats.is_auto(j) for j in jobs] == [True, True, False]
    clock.advance(24 * 3600)
    late = strong(bot, 95, 9)
    await alert(bot, late)
    assert chats.is_auto(late)  # a new day


async def test_each_user_sets_their_own_line_from_the_chat(
    api: FakeApi,
    chats: ChatStore,
    job_store: JobStore,
    conn: sqlite3.Connection,
    queue: TaskQueue,
    clock: FakeClock,
) -> None:
    bot = make_bot(api, chats, job_store, conn, queue, clock, line=None)  # off in .env
    pair(bot, chats, clock)
    await bot.handle_update(message(OWNER, "/autoapply"))
    assert "is <b>off</b>" in api.texts()[-1]
    await bot.handle_update(message(OWNER, "/autoapply 60"))
    assert f"from {MIN_LINE} to 100" in api.texts()[-1]
    assert bot.auto.line() is None
    await bot.handle_update(message(OWNER, "/autoapply 80%"))
    assert "on from <b>80%</b>" in api.texts()[-1]
    job_id = strong(bot, 81)
    await alert(bot, job_id)
    assert chats.is_auto(job_id)
    await bot.handle_update(message(OWNER, "/autoapply off"))
    assert bot.auto.line() is None


async def test_job_boards_are_included_only_when_the_user_says_so(bot: TelegramBot, api: FakeApi) -> None:
    assert not bot.auto.boards()
    await bot.handle_update(message(OWNER, "/autoapply"))
    assert "waits for your Submit" in api.texts()[-1]
    await bot.handle_update(message(OWNER, "/autoapply boards on"))
    assert bot.auto.boards()
    assert "Also on LinkedIn, Naukri" in api.texts()[-1]
    await bot.handle_update(message(OWNER, "/autoapply boards maybe"))
    assert "boards on, or" in api.texts()[-1]
    assert bot.auto.boards()
    await bot.handle_update(message(OWNER, "/autoapply boards off"))
    assert not bot.auto.boards()


async def test_paused_alerts_never_auto_apply(bot: TelegramBot, chats: ChatStore) -> None:
    await bot.handle_update(message(OWNER, "/pause"))
    job_id = strong(bot, 99)
    await alert(bot, job_id)
    assert not chats.is_auto(job_id)


async def test_no_daily_cap_unless_the_user_sets_one(
    api: FakeApi,
    chats: ChatStore,
    job_store: JobStore,
    conn: sqlite3.Connection,
    queue: TaskQueue,
    clock: FakeClock,
) -> None:
    bot = make_bot(api, chats, job_store, conn, queue, clock)
    pair(bot, chats, clock)
    jobs = [strong(bot, 95, n) for n in range(15)]
    for job_id in jobs:
        await alert(bot, job_id)
    assert all(chats.is_auto(j) for j in jobs)
