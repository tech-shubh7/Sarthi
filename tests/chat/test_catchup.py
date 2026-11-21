"""Catch-up mode, out-of-date alerts and the spreadsheet export."""

import csv
import io
import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from sarthi.chat.catchup import ACTIVE_KEY, STALE_AFTER, parse_window
from sarthi.chat.export import HEADER, Row, matches_csv, safe_cell
from sarthi.chat.telegram.bot import TelegramBot
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchRecord, MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue import names
from sarthi.queue.tasks import DeferError, TaskQueue
from tests.chat.test_bot import OWNER, FakeApi, message, pair, tap
from tests.conftest import FakeClock
from tests.matching.conftest import make_job


class FileApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.files: list[tuple[str, bytes, str]] = []

    async def send_document(
        self, chat_id: int, filename: str, content: bytes, caption: str = "", mime: str = "text/csv"
    ) -> int:
        self.files.append((filename, content, caption))
        return 5000


@pytest.fixture
def api() -> FileApi:
    return FileApi()


@pytest.fixture
def chats(conn: sqlite3.Connection, clock: FakeClock) -> ChatStore:
    return ChatStore(conn, clock=clock)


@pytest.fixture
def bot(
    api: FileApi,
    chats: ChatStore,
    job_store: JobStore,
    conn: sqlite3.Connection,
    queue: TaskQueue,
    clock: FakeClock,
) -> TelegramBot:
    return TelegramBot(
        api,  # type: ignore[arg-type]
        chats,
        job_store,
        MatchStore(conn, clock=clock),
        queue,
        profile_version=lambda: "current",
        clock=clock,
    )


def job(
    bot: TelegramBot, n: int, score: int, decision: str = "alert", version: str = "current", **kw: Any
) -> str:
    job_id = bot.jobs.save(
        make_job(url=f"https://boards.greenhouse.io/acme/jobs/{n}", title=f"AI Engineer {n}", **kw)
    ).job_id
    bot.matches.save(
        MatchRecord(job_id, version, score, decision, "strong", None, ("Fits",), ("Check",), False)
    )
    return job_id


def alert_task(job_id: str) -> TaskRecord:
    return TaskRecord(1, names.ALERT, job_id, {}, 1)


# ---- alerts that are out of date ---------------------------------------------------------------------------


async def test_alert_from_an_older_profile_is_not_sent(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    old = job(bot, 1, 90, version="old-profile")
    await bot.handle_alert(alert_task(old))
    assert api.sent == []


async def test_alert_for_a_job_now_hidden_is_not_sent(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    hidden = job(bot, 1, 20, decision="hide")
    await bot.handle_alert(alert_task(hidden))
    await bot.handle_alert(alert_task("no-such-job"))
    assert api.sent == []


async def test_current_strong_alert_is_sent(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    strong = job(bot, 1, 90)
    await bot.handle_alert(alert_task(strong))
    assert len(api.sent) == 1
    assert chats.job_status(strong) is JobStatus.ALERTED


async def test_digest_skips_matches_from_an_older_profile(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    job(bot, 1, 90, version="old-profile")
    fresh = job(bot, 2, 70, decision="digest")
    await bot.handle_digest(TaskRecord(1, names.DIGEST, "d", {}, 1))
    assert chats.job_status(fresh) is JobStatus.DIGESTED
    assert sum("AI Engineer 1" in t for t in api.texts()) == 0


# ---- catch-up --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "hours"),
    [
        ("catchup:24", 24),
        ("catchup:168", 168),
        ("catchup:5", None),
        ("catchup:", None),
        ("catchup:-24", None),
        ("catchup:24;DROP", None),
        ("catchup:9999999999999999", None),
    ],
)
def test_parse_window(data: str, hours: int | None) -> None:
    assert parse_window(data) == hours


async def test_catchup_command_offers_windows(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    await bot.handle_update(message(OWNER, "/catchup"))
    markup = api.sent[-1][2]
    assert markup is not None
    taps = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert taps == ["catchup:24", "catchup:48", "catchup:120", "catchup:168"]


async def test_tapping_a_window_queues_one_catchup(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock, queue: TaskQueue
) -> None:
    pair(bot, chats, clock)
    await bot.handle_update(tap(OWNER, "catchup:48"))
    await bot.handle_update(tap(OWNER, "catchup:48"))  # a second tap while it runs
    assert queue.active(names.CATCHUP) == 1
    assert "already running" in api.texts()[-1]
    assert bot.catchup.running()


async def test_alerts_wait_for_the_batch_while_catching_up(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    bot.catchup.start(24)
    strong = job(bot, 1, 90)
    await bot.handle_alert(alert_task(strong))
    assert api.sent == []
    assert chats.job_status(strong) is None  # still unsent: it comes in the batch


async def test_a_crashed_catchup_stops_holding_alerts(
    bot: TelegramBot, chats: ChatStore, clock: FakeClock
) -> None:
    bot.catchup.start(24)
    clock.advance((STALE_AFTER + timedelta(minutes=1)).total_seconds())
    assert not bot.catchup.running()
    chats.set_state(ACTIVE_KEY, "not a time")
    assert not bot.catchup.running()


def report(hours: int = 48, started: str = "2026-11-20T09:00:00.000000Z", **extra: Any) -> TaskRecord:
    return TaskRecord(2, names.CATCHUP_REPORT, str(hours), {"hours": hours, "started": started, **extra}, 1)


async def test_report_waits_for_matching(
    bot: TelegramBot, chats: ChatStore, clock: FakeClock, queue: TaskQueue
) -> None:
    pair(bot, chats, clock)
    queue.enqueue(names.MATCH, "pending-job")
    with pytest.raises(DeferError):
        await bot.handle_catchup_report(report())


async def test_report_sends_summary_cards_and_spreadsheet(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    bot.catchup.start(48)
    ids = [job(bot, n, 50 + n, decision="alert" if n > 10 else "digest") for n in range(1, 15)]
    job(bot, 99, 10, decision="hide")
    chats.set_job_status(ids[0], JobStatus.SKIPPED)  # already answered: left out
    await bot.handle_catchup_report(report(sources=12, new=30, failed=["feed:remoteok"]))
    summary = api.texts()[0]
    assert "Catch-up: last 2 days" in summary
    assert "Searched 12 source(s), 30 new job(s)." in summary
    assert "feed:remoteok" in summary
    assert "<b>13</b> match your profile (4 strong)" in summary
    assert len(api.sent) == 1 + 10 + 1  # summary + best 10 cards + "apply to the best?"
    ((name, content, _),) = api.files
    assert name.startswith("sarthi-matches-last-2-days-") and name.endswith(".csv")
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows[0] == list(HEADER)
    assert len(rows) == 14
    assert rows[1][0] == "64"  # best first
    assert not bot.catchup.running()


def batch_labels(api: FileApi) -> list[str]:
    markup = api.sent[-1][2]
    assert markup is not None
    return [b["text"] for row in markup["inline_keyboard"] for b in row]


async def report_with(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock, count: int
) -> list[str]:
    pair(bot, chats, clock)
    bot.catchup.start(48)
    ids = [job(bot, n, 90 - n) for n in range(1, count + 1)]  # best first
    await bot.handle_catchup_report(report())
    return ids


async def test_batch_apply_queues_the_best_spaced_out(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock, queue: TaskQueue
) -> None:
    ids = await report_with(bot, api, chats, clock, 12)
    assert batch_labels(api) == ["✅ Apply to the best 5", "✅ Apply to the best 10", "No thanks"]
    chats.set_job_status(ids[1], JobStatus.SKIPPED)  # answered meanwhile: not applied to
    await bot.handle_update(tap(OWNER, "batch:5"))
    assert "Applying to 5 jobs" in api.texts()[-1]
    assert "Nothing is sent until you tap Submit." in api.texts()[-1]
    clock.advance(3600)  # long enough for every spaced-out application to be due
    queued = []
    while (task := queue.claim([names.DOCUMENTS])) is not None:
        queued.append(task)
    assert {t.key for t in queued} == {ids[0], *ids[2:6]}
    assert all(t.payload == {"apply": True, "batch": True} for t in queued)
    assert all(chats.job_status(job_id) is JobStatus.APPLY for job_id in (ids[0], *ids[2:6]))


async def test_batch_applications_are_minutes_apart(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock, queue: TaskQueue
) -> None:
    await report_with(bot, api, chats, clock, 6)
    await bot.handle_update(tap(OWNER, "batch:5"))
    assert queue.claim([names.DOCUMENTS]) is not None  # the first one now
    assert queue.claim([names.DOCUMENTS]) is None  # the next ones later
    clock.advance(4 * 60 + 2 * 60)  # gap + the most jitter
    assert queue.claim([names.DOCUMENTS]) is not None
    assert queue.claim([names.DOCUMENTS]) is None


async def test_batch_is_one_tap_and_no_thanks_applies_nothing(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock, queue: TaskQueue
) -> None:
    await report_with(bot, api, chats, clock, 3)
    assert batch_labels(api) == ["✅ Apply to the best 3", "No thanks"]  # fewer jobs than the smallest size
    await bot.handle_update(tap(OWNER, "batch:0"))
    assert "won't apply" in api.texts()[-1]
    await bot.handle_update(tap(OWNER, "batch:3"))  # an old button tapped again
    assert "expired" in api.texts()[-1]
    assert queue.claim([names.DOCUMENTS]) is None


@pytest.mark.parametrize("data", ["batch:", "batch:abc", "batch:-1", "batch:99", "batch:5:extra"])
async def test_odd_batch_buttons_apply_nothing(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock, queue: TaskQueue, data: str
) -> None:
    await report_with(bot, api, chats, clock, 6)
    await bot.handle_update(tap(OWNER, data))
    assert queue.claim([names.DOCUMENTS]) is None


async def test_strangers_cannot_batch_apply(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock, queue: TaskQueue
) -> None:
    await report_with(bot, api, chats, clock, 6)
    await bot.handle_update(tap(666, "batch:5"))
    assert queue.claim([names.DOCUMENTS]) is None


async def test_report_with_nothing_found(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock
) -> None:
    pair(bot, chats, clock)
    await bot.handle_catchup_report(report(hours=24))
    assert "No jobs in that window match your profile." in api.texts()[-1]
    assert api.files == []


async def test_report_gives_up_waiting_for_a_slow_ai(
    bot: TelegramBot, api: FileApi, chats: ChatStore, clock: FakeClock, queue: TaskQueue
) -> None:
    pair(bot, chats, clock)
    queue.enqueue(names.MATCH, "slow")
    clock.advance(3600)
    await bot.handle_catchup_report(report())
    assert "still being checked" in api.texts()[0]


async def test_report_waits_for_pairing(bot: TelegramBot) -> None:
    with pytest.raises(DeferError):
        await bot.handle_catchup_report(report())


# ---- spreadsheet safety ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('=HYPERLINK("http://evil","x")', '\'=HYPERLINK("http://evil","x")'),
        ("+1-555", "'+1-555"),
        ("-2+3", "'-2+3"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tTab", "'\tTab"),
        ("Backend Engineer", "Backend Engineer"),
        ("a\x00b", "ab"),
        ("x" * 5000, "x" * 1000),
    ],
)
def test_safe_cell(value: str, expected: str) -> None:
    assert safe_cell(value) == expected


def test_csv_rows_are_text_and_unicode_survives() -> None:
    posting = make_job(title="=cmd|' /C calc'!A0", company="Zürich AG", location_text="München")
    record = MatchRecord("j", "v", 77, "alert", "strong", None, ("Python, FastAPI",), (), False)
    rows = list(csv.reader(io.StringIO(matches_csv([Row(posting, record)]).decode("utf-8-sig"))))
    assert rows[1][2].startswith("'=")
    assert rows[1][3] == "Zürich AG"
    assert rows[1][1] == "Strong"
