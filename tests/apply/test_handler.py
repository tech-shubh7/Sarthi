"""The apply and submit tasks (with a fake browser), the application record and the approval in the chat."""

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sarthi.apply.adapters import FillError, PostingClosedError
from sarthi.apply.engine import EnginePausedError, FillResult, SubmitResult
from sarthi.apply.handler import Applier, needs_login, site_of
from sarthi.apply.linkedin import LimitReachedError, LoginNeededError
from sarthi.chat.applying import ApplyingFlow, preview_text
from sarthi.db.answers import AnswerStore
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.documents import DocumentRecord, DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.jobs.forms import FormField
from sarthi.profile.models import Preferences, Profile
from sarthi.queue import names
from sarthi.queue.tasks import DeferError, TaskQueue
from tests.chat.test_catchup import FileApi
from tests.conftest import FakeClock
from tests.matching.conftest import make_job


class FakeEngine:
    def __init__(self, result: FillResult | Exception, outcome: SubmitResult | None = None) -> None:
        self.result = result
        self.outcome = outcome or SubmitResult("submitted", "Confirmation seen")
        self.closed: list[str] = []
        self.filled: list[str] = []
        self.walks = False  # no application browser: boards are handed over
        self.one_click: list[bool] = []

    async def fill(self, job_id: str, url: str, ctx: Any, *, one_click: bool = False) -> FillResult:
        self.filled.append(url)
        self.one_click.append(one_click)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def submit(
        self, job_id: str, code: str, *, before_click: Callable[[], None] | None = None
    ) -> SubmitResult:
        return self.outcome

    async def close_job(self, job_id: str) -> None:
        self.closed.append(job_id)

    async def watch(self, job_id: str) -> SubmitResult:
        return self.outcome


PROFILE = Profile(preferences=Preferences(roles=("Backend Engineer",), home_country="IN"), resume_text="x")


def applier(
    conn: sqlite3.Connection,
    jobs: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    engine: FakeEngine,
    clock: FakeClock,
    limit: int = 30,
) -> Applier:
    return Applier(
        engine,  # type: ignore[arg-type]
        jobs,
        DocumentStore(conn),
        AnswerStore(conn),
        ApplicationStore(conn, clock=clock),
        queue=queue,
        load_profile=lambda: PROFILE,
        data_dir=tmp_path,
        daily_limit=limit,
        clock=clock,
    )


def job_at(
    jobs: JobStore,
    url: str,
    title: str = "Backend Engineer",
    company: str = "Acme",
    country: str = "IN",
    *,
    docs: bool = True,
) -> str:
    posting = make_job(url=url, apply_url=url, title=title, company=company, country_codes=[country])
    job_id = jobs.save(posting).job_id
    if docs:  # the tailored resume is made before applying
        made_for = PROFILE.version
        record = DocumentRecord(job_id, made_for, f"documents/{job_id}/r.pdf", f"documents/{job_id}/c.pdf")
        DocumentStore(jobs._conn).save(record)
    return job_id


async def test_a_resume_made_from_an_older_upload_is_made_again_before_applying(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER, docs=False)
    old = DocumentRecord(job_id, "an-older-profile", f"documents/{job_id}/r.pdf", f"documents/{job_id}/c.pdf")
    DocumentStore(conn).save(old)
    engine = FakeEngine(FILLED)
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    assert engine.filled == []  # nothing filled with the old resume
    assert payload(queue, names.DOCUMENTS) == {"apply": True}  # a new one is made first


def payload(queue: TaskQueue, kind: str) -> dict[str, Any]:
    task = queue.claim([kind])
    assert task is not None, kind
    return task.payload


FILLED = FillResult("lever", filled=[("Full name", "Asha Rao")], code="ABC234")
LEVER = "https://jobs.lever.co/acme/1"


async def test_a_clean_fill_waits_for_the_users_approval(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER)
    await applier(conn, job_store, queue, tmp_path, FakeEngine(FILLED), clock).handle_apply(
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    preview = payload(queue, names.APPLY_PREVIEW)
    assert preview["code"] == "ABC234"
    assert preview["filled"] == [["Full name", "Asha Rao"]]  # stored as JSON
    record = ApplicationStore(conn).get(job_id)
    assert record is not None
    assert (record.status, record.site) == (ApplicationStatus.FILLED, "jobs.lever.co")


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1", "Workday"),
        ("https://in.indeed.com/viewjob?jk=1", "logged-in account"),
        ("https://www.glassdoor.co.in/job-listing/x", "logged-in account"),
    ],
)
async def test_forms_needing_a_person_are_handed_over(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
    url: str,
    why: str,
) -> None:
    engine = FakeEngine(FILLED)
    job_id = job_at(job_store, url)
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    result = payload(queue, names.APPLY_RESULT)
    assert result["outcome"] == "needs_human"
    assert why in result["detail"]
    assert engine.filled == []  # no browser was opened


@pytest.mark.parametrize(
    ("fill", "kind"),
    [
        (FillError("the form did not load: timeout"), "could not be filled"),
        (FillResult("lever", captcha=True, code="X"), "CAPTCHA"),
        (FillResult("lever", failed=[("Location", "no option matching")], code="X"), "could not be entered"),
    ],
)
async def test_problems_hand_over_with_the_reason(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
    fill: Any,
    kind: str,
) -> None:
    job_id = job_at(job_store, LEVER)
    await applier(conn, job_store, queue, tmp_path, FakeEngine(fill), clock).handle_apply(
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    assert kind in payload(queue, names.APPLY_RESULT)["detail"]


async def test_questions_found_in_the_browser_are_asked(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    blank = FormField("s-9", "Why us?", "textarea", True)
    job_id = job_at(job_store, LEVER)
    engine = FakeEngine(FillResult("lever", blanks=[blank], code="X"))
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    assert AnswerStore(conn).pending(job_id) == [blank]
    assert queue.active(names.ASK_QUESTIONS) == 1
    assert engine.closed == [job_id]


async def test_one_application_per_role(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    first = job_at(job_store, LEVER)
    again = job_at(
        job_store, "https://boards.greenhouse.io/acme/jobs/2", country="GB"
    )  # same role, another post
    assert again != first
    ApplicationStore(conn, clock=clock).record(first, ApplicationStatus.SUBMITTED, site="jobs.lever.co")
    engine = FakeEngine(FILLED)
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(
        TaskRecord(1, names.APPLY, again, {}, 1)
    )
    assert payload(queue, names.APPLY_RESULT)["outcome"] == "duplicate"
    assert engine.filled == []


async def test_daily_limit_per_site(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    store = ApplicationStore(conn, clock=clock)
    done = job_at(job_store, "https://jobs.lever.co/acme/9", title="Data Engineer")
    store.record(done, ApplicationStatus.SUBMITTED, site="jobs.lever.co")
    job_id = job_at(job_store, LEVER)
    with pytest.raises(DeferError):
        await applier(conn, job_store, queue, tmp_path, FakeEngine(FILLED), clock, limit=1).handle_apply(
            TaskRecord(1, names.APPLY, job_id, {}, 1)
        )
    clock.advance(25 * 3600)  # a day later the limit is free again
    await applier(conn, job_store, queue, tmp_path, FakeEngine(FILLED), clock, limit=1).handle_apply(
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    assert queue.active(names.APPLY_PREVIEW) == 1


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (SubmitResult("submitted", "Confirmation seen"), ApplicationStatus.SUBMITTED),
        (SubmitResult("needs_human", "CAPTCHA"), ApplicationStatus.NEEDS_HUMAN),
        (SubmitResult("failed", "No confirmation"), ApplicationStatus.FAILED),
        (SubmitResult("verification_failed", "Lever refused"), ApplicationStatus.FAILED),
        (SubmitResult("unknown", "Pressed, no confirmation"), ApplicationStatus.NEEDS_HUMAN),
    ],
)
async def test_submit_records_how_it_ended(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
    outcome: SubmitResult,
    status: ApplicationStatus,
) -> None:
    job_id = job_at(job_store, LEVER)
    await applier(conn, job_store, queue, tmp_path, FakeEngine(FILLED, outcome), clock).handle_submit(
        TaskRecord(1, names.SUBMIT, job_id, {"code": "ABC234"}, 1)
    )
    record = ApplicationStore(conn).get(job_id)
    assert record is not None
    assert record.status is status
    assert (record.submitted_at is not None) is (status is ApplicationStatus.SUBMITTED)
    assert payload(queue, names.APPLY_RESULT)["outcome"] == outcome.outcome


async def test_an_expired_approval_fills_again(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER)
    engine = FakeEngine(FILLED, SubmitResult("expired", "That approval is no longer valid."))
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_submit(
        TaskRecord(1, names.SUBMIT, job_id, {"code": "OLD"}, 1)
    )
    assert queue.active(names.APPLY) == 1


@pytest.mark.parametrize(
    ("url", "site", "login"),
    [
        ("https://jobs.lever.co/acme/1", "jobs.lever.co", False),
        (
            "https://www.linkedin.com/jobs/view/1",
            "www.linkedin.com",
            False,
        ),  # Easy Apply with the saved login
        ("https://www.naukri.com/job-listings-1", "www.naukri.com", False),
        ("https://in.indeed.com/viewjob?jk=1", "in.indeed.com", True),
        ("not a url", "", False),
    ],
)
def test_sites(url: str, site: str, login: bool) -> None:
    assert site_of(url) == site
    assert needs_login(url) is login


# ---- the chat side --------------------------------------------------------------------------------------

OWNER = 111


@pytest.fixture
def flow(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> ApplyingFlow:
    return ApplyingFlow(
        FileApi(),
        ChatStore(conn, clock=clock),
        job_store,
        ApplicationStore(conn, clock=clock),
        queue,
        data_dir=tmp_path,
    )  # type: ignore[arg-type]


class PhotoApi(FileApi):
    def __init__(self) -> None:
        super().__init__()
        self.photos: list[tuple[bytes, str, Any]] = []

    async def send_photo(self, chat_id: int, photo: bytes, caption: str = "", markup: Any = None) -> int:
        self.photos.append((photo, caption, markup))
        return 7


async def test_preview_sends_the_screenshot_with_submit_and_cancel(
    flow: ApplyingFlow, job_store: JobStore, tmp_path: Path
) -> None:
    flow.api = PhotoApi()  # type: ignore[assignment]
    (tmp_path / "screenshots").mkdir()
    (tmp_path / "screenshots" / "a.png").write_bytes(b"\x89PNG")
    job_id = job_at(job_store, LEVER)
    await flow.preview(
        TaskRecord(
            1,
            names.APPLY_PREVIEW,
            job_id,
            {"code": "ABC234", "screenshot": "screenshots/a.png", "filled": [["Name", "<b>Asha</b>"]]},
            1,
        ),
        OWNER,
    )
    photo, caption, markup = flow.api.photos[-1]  # type: ignore[attr-defined]
    assert photo == b"\x89PNG"
    assert "&lt;b&gt;Asha&lt;/b&gt;" in caption
    assert [b["callback_data"] for row in markup["inline_keyboard"] for b in row] == [
        f"submit:{job_id}:ABC234",
        f"cancel:{job_id}",
    ]


@pytest.mark.parametrize("shot", ["../../etc/passwd", "/etc/passwd", "screenshots/missing.png", ""])
async def test_screenshots_only_from_the_data_folder(
    flow: ApplyingFlow, job_store: JobStore, shot: str
) -> None:
    job_id = job_at(job_store, LEVER)
    await flow.preview(
        TaskRecord(1, names.APPLY_PREVIEW, job_id, {"code": "ABC234", "screenshot": shot, "filled": []}, 1),
        OWNER,
    )
    api: Any = flow.api
    assert "Ready to apply" in api.texts()[-1]  # sent as text, never another file


async def test_taps(flow: ApplyingFlow, job_store: JobStore, queue: TaskQueue) -> None:
    job_id = job_at(job_store, LEVER)
    assert await flow.on_button(OWNER, f"submit:{job_id}:ABC234") == "⏳ Submitting…"
    assert payload(queue, names.SUBMIT) == {"code": "ABC234"}
    assert await flow.on_button(OWNER, f"cancel:{job_id}") == "Cancelled: nothing was sent."
    assert flow.chats.job_status(job_id) is JobStatus.SKIPPED
    assert await flow.on_button(OWNER, f"applied:{job_id}") == "✅ Marked as applied"
    record = flow.applications.get(job_id)
    assert record is not None
    assert record.status is ApplicationStatus.APPLIED_BY_USER
    before = queue.store.task_counts()
    assert await flow.on_button(OWNER, "submit:no-such-job:X") == "That job is no longer stored."
    assert await flow.on_button(OWNER, f"submit:{job_id}:") == ""  # no code: nothing is submitted
    assert queue.store.task_counts() == before


async def test_results(flow: ApplyingFlow, job_store: JobStore) -> None:
    job_id = job_at(job_store, LEVER)
    api: Any = flow.api
    await flow.result(
        TaskRecord(
            1, names.APPLY_RESULT, job_id, {"outcome": "needs_human", "detail": "Workday", "url": LEVER}, 1
        ),
        OWNER,
    )
    assert "needs you: Workday" in api.texts()[-1]
    assert api.sent[-1][2]["inline_keyboard"][0][0]["callback_data"] == f"applied:{job_id}"
    await flow.result(
        TaskRecord(1, names.APPLY_RESULT, job_id, {"outcome": "needs_human", "detail": "has a CAPTCHA"}, 1),
        OWNER,
    )
    assert "Finish it myself" in api.texts()[-1] and "only you can solve" in api.texts()[-1]
    rows = api.sent[-1][2]["inline_keyboard"]
    assert [rows[0][0]["callback_data"], rows[1][0]["callback_data"]] == [
        f"finish:{job_id}",
        f"applied:{job_id}",
    ]
    await flow.result(
        TaskRecord(1, names.APPLY_RESULT, job_id, {"outcome": "submitted", "detail": "Confirmation seen"}, 1),
        OWNER,
    )
    assert api.texts()[-1].startswith("✅ Applied:")
    assert flow.chats.job_status(job_id) is JobStatus.APPLIED


def test_preview_text_is_short_and_escaped() -> None:
    filled = [[f"Field {i}", "<script>"] for i in range(20)]
    text = preview_text("A & B", "Dev <1>", filled)
    assert "A &amp; B" in text and "Dev &lt;1&gt;" in text
    assert "and 8 more" in text
    assert "<script>" not in text


async def test_the_tailored_resume_comes_before_applying(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER, docs=False)  # e.g. Apply tapped before documents existed
    engine = FakeEngine(FILLED)
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    assert payload(queue, names.DOCUMENTS) == {"apply": True}
    assert engine.filled == []


async def test_a_missing_login_tells_the_user_how_to_log_in(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, "https://www.linkedin.com/jobs/view/1")
    engine = FakeEngine(
        LoginNeededError("LinkedIn needs you to log in once: run `uv run sarthi login linkedin`")
    )
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    result = payload(queue, names.APPLY_RESULT)
    assert result["outcome"] == "needs_human"
    assert "sarthi login linkedin" in result["detail"]


async def test_the_sites_own_daily_limit_waits(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, "https://www.linkedin.com/jobs/view/1")
    engine = FakeEngine(LimitReachedError("LinkedIn's daily Easy Apply limit is reached"))
    with pytest.raises(DeferError):
        await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(
            TaskRecord(1, names.APPLY, job_id, {}, 1)
        )


def auto_applier(
    conn: sqlite3.Connection,
    jobs: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    engine: FakeEngine,
    clock: FakeClock,
) -> tuple[Applier, ChatStore]:
    chats = ChatStore(conn, clock=clock)
    base = applier(conn, jobs, queue, tmp_path, engine, clock)
    base.chats = chats
    return base, chats


async def test_auto_apply_sends_a_clean_fill_without_a_tap(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER)
    worker, chats = auto_applier(conn, job_store, queue, tmp_path, FakeEngine(FILLED), clock)
    chats.set_auto(job_id, on=True)
    await worker.handle_apply(TaskRecord(1, names.APPLY, job_id, {}, 1))
    assert queue.claim([names.APPLY_PREVIEW]) is None
    submit = queue.claim([names.SUBMIT])
    assert submit is not None
    assert submit.payload == {"code": "ABC234", "auto": True}
    assert not chats.is_auto(job_id)  # one try only
    await worker.handle_submit(submit)
    result = payload(queue, names.APPLY_RESULT)
    assert result["outcome"] == "submitted"
    assert result["detail"].startswith("🤖 Sent by auto-apply.")


async def test_auto_apply_never_sends_on_a_job_board_or_with_questions_left(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    engine = FakeEngine(FILLED)
    engine.walks = True  # boards are walked by the application browser
    board = job_at(job_store, "https://in.indeed.com/viewjob?jk=7")
    worker, chats = auto_applier(conn, job_store, queue, tmp_path, engine, clock)
    chats.set_auto(board, on=True)
    await worker.handle_apply(TaskRecord(1, names.APPLY, board, {}, 1))
    assert queue.claim([names.SUBMIT]) is None
    assert queue.claim([names.APPLY_PREVIEW]) is not None  # the usual approval
    blank = FormField("q1", "Why us?", "textarea", True)
    asks = job_at(job_store, "https://jobs.lever.co/beta/2", company="Beta")
    worker.engine = FakeEngine(FillResult("lever", filled=[("Name", "Asha")], blanks=[blank], code="X"))  # type: ignore[assignment]
    chats.set_auto(asks, on=True)
    await worker.handle_apply(TaskRecord(2, names.APPLY, asks, {}, 1))
    assert queue.claim([names.SUBMIT]) is None
    assert not chats.is_auto(asks)  # the user answers, so the user approves


async def test_auto_apply_sends_on_a_job_board_when_the_user_included_them(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    engine = FakeEngine(FILLED)
    engine.walks = True
    board = job_at(job_store, "https://in.indeed.com/viewjob?jk=8")
    worker, chats = auto_applier(conn, job_store, queue, tmp_path, engine, clock)
    worker.auto_on_boards = lambda: True
    chats.set_auto(board, on=True)
    await worker.handle_apply(TaskRecord(1, names.APPLY, board, {}, 1))
    submit = queue.claim([names.SUBMIT])
    assert submit is not None and submit.payload["auto"] is True
    assert queue.claim([names.APPLY_PREVIEW]) is None


@pytest.mark.parametrize(
    ("outcome", "status", "detail"),
    [
        (SubmitResult("submitted", "You sent it"), ApplicationStatus.SUBMITTED, "You sent it"),
        (
            SubmitResult("needs_human", "Not sent within 10 minutes."),
            ApplicationStatus.NEEDS_HUMAN,
            "CAPTCHA",
        ),
    ],
)
async def test_finishing_by_hand_records_what_the_user_did(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
    outcome: SubmitResult,
    status: ApplicationStatus,
    detail: str,
) -> None:
    job_id = job_at(job_store, LEVER)
    engine = FakeEngine(FillResult("lever", captcha=True, code="X"), outcome)
    done = await applier(conn, job_store, queue, tmp_path, engine, clock).finish_by_hand(job_id, engine)  # type: ignore[arg-type]
    assert done == outcome
    assert engine.filled == [LEVER]
    record = ApplicationStore(conn).get(job_id)
    assert record is not None
    assert record.status is status
    assert detail in record.detail
    sent = payload(queue, names.APPLY_RESULT)
    assert sent["outcome"] == outcome.outcome and sent["url"] == LEVER
    assert detail in sent["detail"]  # not sent: the chat offers Finish it myself again


async def test_finishing_a_job_no_longer_stored_does_nothing(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    engine = FakeEngine(FILLED)
    assert (
        await applier(conn, job_store, queue, tmp_path, engine, clock).finish_by_hand("gone", engine) is None
    )  # type: ignore[arg-type]
    assert engine.filled == []


async def test_a_job_already_sent_is_never_filled_or_submitted_again(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER)
    ApplicationStore(conn, clock=clock).record(job_id, ApplicationStatus.SUBMITTED, site="jobs.lever.co")
    engine = FakeEngine(FILLED)
    flow = applier(conn, job_store, queue, tmp_path, engine, clock)
    await flow.handle_apply(TaskRecord(1, names.APPLY, job_id, {}, 1))  # tapped Apply again
    assert engine.filled == []
    assert payload(queue, names.APPLY_RESULT)["outcome"] == "duplicate"
    engine.outcome = SubmitResult("failed", "should never be pressed")
    await flow.handle_submit(TaskRecord(2, names.SUBMIT, job_id, {"code": "ABC234"}, 1))
    record = ApplicationStore(conn).get(job_id)
    assert record is not None
    assert record.status is ApplicationStatus.SUBMITTED


async def test_a_closed_posting_is_recorded_as_closed_not_handed_over(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER)
    engine = FakeEngine(PostingClosedError("the posting is closed (jobs.lever.co answered 404)"))
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    record = ApplicationStore(conn).get(job_id)
    assert record is not None
    assert record.status is ApplicationStatus.FAILED
    assert "closed" in record.detail
    assert payload(queue, names.APPLY_RESULT)["outcome"] == "closed"


async def test_a_paused_browser_defers_the_fill(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER)
    engine = FakeEngine(EnginePausedError("a window is open"))
    with pytest.raises(DeferError):
        await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(
            TaskRecord(1, names.APPLY, job_id, {}, 1)
        )
    assert ApplicationStore(conn).get(job_id) is None  # no false hand-over


@pytest.mark.parametrize(
    "url", ["https://www.linkedin.com/jobs/view/1", "https://www.naukri.com/job-listings-1"]
)
async def test_native_boards_respect_the_automatic_send_switch(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
    url: str,
) -> None:
    job_id = job_at(job_store, url)
    worker = applier(conn, job_store, queue, tmp_path, FakeEngine(FILLED), clock)
    worker.chats = ChatStore(conn, clock=clock)
    worker.chats.set_auto(job_id, on=True)
    await worker.handle_apply(TaskRecord(1, names.APPLY, job_id, {}, 1))
    assert queue.active(names.SUBMIT) == 0
    assert payload(queue, names.APPLY_PREVIEW)["code"] == FILLED.code


async def test_manual_only_boards_never_reach_the_browser(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, "https://www.naukri.com/job-listings-1")
    engine = FakeEngine(FILLED)
    worker = applier(conn, job_store, queue, tmp_path, engine, clock)
    worker.allow_board_automation = False
    await worker.handle_apply(TaskRecord(1, names.APPLY, job_id, {}, 1))
    assert engine.filled == []
    record = worker.applications.get(job_id)
    assert record is not None and record.status is ApplicationStatus.NEEDS_HUMAN


async def test_uncertain_submission_stays_held_on_refill_and_submit(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER)
    engine = FakeEngine(FILLED, SubmitResult("unknown", "Natural-language confirmation missing."))
    worker = applier(conn, job_store, queue, tmp_path, engine, clock)
    submit = TaskRecord(1, names.SUBMIT, job_id, {"code": "ABC234"}, 1)
    await worker.handle_submit(submit)
    worker.applications.record(job_id, ApplicationStatus.FAILED, detail="A stale document task failed")
    engine.outcome = SubmitResult("submitted", "Confirmation seen")
    await worker.handle_apply(TaskRecord(2, names.APPLY, job_id, {}, 1))
    await worker.handle_submit(submit)
    assert engine.filled == []
    record = worker.applications.get(job_id)
    assert record is not None and record.unconfirmed and record.submitted_at is None


async def test_submit_rechecks_the_site_limit_after_a_form_was_filled(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    pending = job_at(job_store, LEVER)
    earlier = job_at(job_store, "https://jobs.lever.co/other/2", company="Other")
    worker = applier(conn, job_store, queue, tmp_path, FakeEngine(FILLED), clock, limit=1)
    worker.applications.record(pending, ApplicationStatus.FILLED, site="jobs.lever.co")
    worker.applications.record(earlier, ApplicationStatus.SUBMITTED, site="jobs.lever.co")
    with pytest.raises(DeferError):
        await worker.handle_submit(TaskRecord(1, names.SUBMIT, pending, {"code": "ABC234"}, 1))
    record = worker.applications.get(pending)
    assert record is not None and record.status is ApplicationStatus.FILLED


@pytest.mark.parametrize("refill_before", [False, True])
async def test_automatic_refill_is_allowed_once(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
    refill_before: bool,
) -> None:
    job_id = job_at(job_store, LEVER)
    engine = FakeEngine(FILLED, SubmitResult("expired", "The page closed before Submit."))
    worker = applier(conn, job_store, queue, tmp_path, engine, clock)
    worker.applications.record(job_id, ApplicationStatus.FILLED, site="jobs.lever.co")
    original = {"code": "OLD", "auto": True, "auto_refill": refill_before}
    await worker.handle_submit(TaskRecord(1, names.SUBMIT, job_id, original, 1))
    refill = queue.claim([names.APPLY])
    assert refill is not None and refill.payload.get("auto_refill", False) is not refill_before
    await worker.handle_apply(refill)
    if refill_before:
        assert queue.active(names.SUBMIT) == 0
        assert queue.active(names.APPLY_PREVIEW) == 1
    else:
        assert payload(queue, names.SUBMIT) == {"code": FILLED.code, "auto": True, "auto_refill": True}


async def test_submit_checks_for_a_duplicate_role_after_filling(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    first = job_at(job_store, LEVER)
    next_job = job_at(job_store, "https://boards.greenhouse.io/acme/jobs/2", country="GB")
    engine = FakeEngine(FILLED)
    worker = applier(conn, job_store, queue, tmp_path, engine, clock)
    worker.applications.record(next_job, ApplicationStatus.FILLED, site="boards.greenhouse.io")
    worker.applications.record(first, ApplicationStatus.SUBMITTED, site="jobs.lever.co")
    await worker.handle_submit(TaskRecord(1, names.SUBMIT, next_job, {"code": FILLED.code, "auto": True}, 1))
    assert payload(queue, names.APPLY_RESULT)["outcome"] == "duplicate"
    assert engine.closed == [next_job]
    assert not worker.applications.was_sent(next_job)


async def test_skip_stops_a_queued_auto_submit(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    job_id = job_at(job_store, LEVER)
    engine = FakeEngine(FILLED)
    worker, chats = auto_applier(conn, job_store, queue, tmp_path, engine, clock)
    chats.set_job_status(job_id, JobStatus.SKIPPED)
    await worker.handle_submit(TaskRecord(1, names.SUBMIT, job_id, {"code": FILLED.code, "auto": True}, 1))
    assert engine.closed == [job_id]
    assert not worker.applications.was_sent(job_id)


async def test_interrupted_press_is_held_after_restart(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_at(job_store, LEVER)
    applications = ApplicationStore(conn, clock=clock)
    applications.record(job_id, ApplicationStatus.FILLED, site="jobs.lever.co")
    applications.mark_press_started(job_id)
    worker = applier(conn, job_store, queue, tmp_path, FakeEngine(FILLED), clock)
    await worker.handle_submit(TaskRecord(1, names.SUBMIT, job_id, {"code": "old", "auto": True}, 1))
    record = applications.get(job_id)
    assert record is not None and record.unconfirmed and record.submitted_at is None
    assert not applications.press_unresolved(job_id)
    assert payload(queue, names.APPLY_RESULT)["outcome"] == "unknown"
