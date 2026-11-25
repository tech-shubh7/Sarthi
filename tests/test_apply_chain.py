"""A started application finishes: priorities, dead chains restarted, lost fills, failed documents."""

import sqlite3
from pathlib import Path

from sarthi.apply.engine import SubmitResult
from sarthi.chat.applying import PENDING, ApplyingFlow
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue import names
from sarthi.queue.tasks import Priority, TaskQueue
from tests.apply.test_handler import FILLED, LEVER, FakeEngine, applier, job_at, payload
from tests.conftest import FakeClock


def _priority(conn: sqlite3.Connection, kind: str, key: str) -> int:
    return int(
        conn.execute("SELECT priority FROM task WHERE type = ? AND key = ?", (kind, key)).fetchone()[0]
    )


async def test_an_approved_submit_on_a_closed_page_carries_over_once(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER)
    expired = FakeEngine(FILLED, SubmitResult("expired", "The form was closed."))
    await applier(conn, job_store, queue, tmp_path, expired, clock).handle_submit(
        TaskRecord(1, names.SUBMIT, job_id, {"code": "OLD123"}, 1)
    )
    refill = queue.claim([names.APPLY])
    assert refill is not None and refill.payload == {"approved": True}
    assert _priority(conn, names.APPLY, job_id) == Priority.APPLY  # under way: before new alerts
    assert payload(queue, names.APPLY_RESULT)["approved"] is True

    # the refill is complete: sent without asking again (the user already approved this form)
    await applier(conn, job_store, queue, tmp_path, FakeEngine(FILLED), clock).handle_apply(refill)
    queue.complete(refill)
    assert payload(queue, names.SUBMIT) == {"code": "ABC234", "approved": True}
    assert queue.claim([names.APPLY_PREVIEW]) is None

    # that one expired too: back to a normal preview, never a loop
    await applier(conn, job_store, queue, tmp_path, expired, clock).handle_submit(
        TaskRecord(2, names.SUBMIT, job_id, {"code": "ABC234", "approved": True}, 1)
    )
    assert payload(queue, names.APPLY) == {"approved": False}


def _flow(conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path) -> ApplyingFlow:
    chats = ChatStore(conn)
    return ApplyingFlow(None, chats, job_store, ApplicationStore(conn), queue, data_dir=tmp_path)  # type: ignore[arg-type]


async def test_a_stale_submit_never_voids_the_newest_fill_and_closed_fills_are_dropped(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path
) -> None:
    flow = _flow(conn, job_store, queue, tmp_path)
    job_id, other = (
        job_at(job_store, LEVER),
        job_at(job_store, "https://jobs.lever.co/acme/2", title="Data Engineer"),
    )
    flow.chats.set_state(PENDING + job_id, '{"code": "NEW456", "screenshot": ""}')
    flow.chats.set_state(PENDING + other, '{"code": "GONE11", "screenshot": ""}')
    assert "newest" in await flow.on_button(1, f"submit:{job_id}:OLD123")
    assert queue.claim([names.SUBMIT]) is None and job_id in flow.waiting()
    assert flow.drop_closed(lambda job, code: code == "NEW456") == [other]
    assert list(flow.waiting()) == [job_id]
    assert await flow.on_button(1, f"submit:{job_id}:NEW456") == "⏳ Submitting…"
    assert _priority(conn, names.SUBMIT, job_id) == Priority.APPLY


async def test_a_second_apply_tap_restarts_a_dead_chain_only(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    from sarthi.chat.telegram.bot import TelegramBot  # noqa: PLC0415
    from sarthi.db.matches import MatchStore  # noqa: PLC0415
    from tests.chat.test_bot import FakeApi  # noqa: PLC0415

    chats = ChatStore(conn, clock=clock)
    bot = TelegramBot(FakeApi(), chats, job_store, MatchStore(conn), queue, clock=clock)  # type: ignore[arg-type]
    job_id = job_at(job_store, LEVER)
    assert bot.choose(job_id, "apply") is JobStatus.APPLY
    first = queue.claim([names.DOCUMENTS])
    assert first is not None
    assert bot.choose(job_id, "apply") is JobStatus.APPLY  # running: a second tap changes nothing
    assert queue.claim([names.DOCUMENTS]) is None
    queue.store.fail(first.id, "gave up", None)  # the chain died
    bot.choose(job_id, "apply")
    restarted = queue.claim([names.DOCUMENTS])
    assert restarted is not None and restarted.payload == {"apply": True}
    queue.complete(restarted)
    ApplicationStore(conn).record(job_id, ApplicationStatus.SUBMITTED)
    bot.choose(job_id, "apply")  # sent already: nothing to restart
    assert queue.claim([names.DOCUMENTS]) is None


async def test_documents_that_fail_stop_the_apply_with_a_reason(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    from sarthi.chat.telegram.bot import TelegramBot  # noqa: PLC0415
    from sarthi.db.answers import AnswerStore  # noqa: PLC0415
    from sarthi.db.matches import MatchStore  # noqa: PLC0415
    from tests.chat.test_bot import FakeApi  # noqa: PLC0415

    chats = ChatStore(conn, clock=clock)
    bot = TelegramBot(
        FakeApi(), chats, job_store, MatchStore(conn), queue, answers=AnswerStore(conn),  # type: ignore[arg-type]
        applications=ApplicationStore(conn), data_dir=tmp_path, clock=clock,
    )  # fmt: skip
    job_id = job_at(job_store, LEVER, docs=False)
    error = {"apply": True, "error": "The AI could not read your resume."}
    await bot.handle_documents_ready(TaskRecord(1, names.DOCUMENTS_READY, job_id, error, 1))
    record = ApplicationStore(conn).get(job_id)
    assert record is not None and record.status is ApplicationStatus.FAILED
    assert "could not read your resume" in record.detail
    assert queue.claim([names.FORM_CHECK]) is None
    chats.set_job_status(job_id, JobStatus.APPLY)
    bot.choose(job_id, "apply")  # failed documents: Apply again tries again
    assert queue.claim([names.DOCUMENTS]) is not None
