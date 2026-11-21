import sqlite3
from typing import Any

import pytest

from sarthi.chat.source_notice import source_label, source_notice_text
from sarthi.chat.telegram.bot import TelegramBot
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue.tasks import TaskQueue
from tests.chat.test_bot import OWNER, FakeApi
from tests.conftest import FakeClock


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("boards:linkedin+indeed", "Linkedin + Indeed search"),
        ("greenhouse:razorpay", "razorpay (greenhouse)"),
        ("feed:remotive", "the remotive feed"),
        ("catalog:ats-scrapers", "the company-board snapshot (~192k companies)"),
        ("boards:adzuna", "Adzuna"),
        ("page:https://acme.example/careers", "https://acme.example/careers"),
        ("odd", "odd"),
    ],
)
def test_source_labels(name: str, label: str) -> None:
    assert source_label(name) == label


def test_down_up_and_permanent_messages() -> None:
    down = source_notice_text({"source": "feed:remotive", "status": "down", "streak": 3, "error": "HTTP 503"})
    assert "failed 3 times in a row (HTTP 503)" in down
    assert "keeps trying" in down
    assert (
        source_notice_text({"source": "feed:remotive", "status": "up"}) == "✅ the remotive feed works again."
    )
    bad_key = source_notice_text(
        {"source": "boards:adzuna", "status": "down", "permanent": True, "error": "x"}
    )
    assert "Check its key in .env." in bad_key
    bad_slug = source_notice_text(
        {"source": "greenhouse:acme", "status": "down", "permanent": True, "error": "x"}
    )
    assert "Check the setting that names it in .env." in bad_slug


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "<b>x</b>", "status": "down", "streak": 3, "error": "<script>alert(1)</script>"},
        {},
        {"source": None, "status": None, "streak": "not a number?"[:0], "error": None},
    ],
)
def test_odd_payloads_never_break_or_inject(payload: dict[str, Any]) -> None:
    text = source_notice_text(payload)
    assert "<script>" not in text
    assert "<b>" not in text


async def test_the_bot_sends_it_to_the_owner_only_when_paired(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock
) -> None:
    api, chats = FakeApi(), ChatStore(conn, clock=clock)
    bot = TelegramBot(api, chats, job_store, MatchStore(conn, clock=clock), queue, clock=clock)  # type: ignore[arg-type]
    task = TaskRecord(1, "source_notice", "feed:x:up", {"source": "feed:x", "status": "up"}, 1)
    await bot.handle_source_notice(task)
    assert api.sent == []  # not paired: nobody to tell
    chats.set_state("telegram.owner_chat_id", str(OWNER))
    await bot.handle_source_notice(task)
    assert api.sent == [(OWNER, "✅ the x feed works again.", None)]
