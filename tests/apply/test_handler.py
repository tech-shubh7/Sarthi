"""The apply and submit tasks (with a fake browser), the application record and the approval in the chat."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from sarthi.apply.adapters import FillError
from sarthi.apply.engine import FillResult, SubmitResult
from sarthi.apply.handler import Applier, needs_login, site_of
from sarthi.chat.applying import ApplyingFlow, preview_text
from sarthi.db.answers import AnswerStore
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.documents import DocumentStore
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

    async def fill(self, job_id: str, url: str, ctx: Any) -> FillResult:
        self.filled.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def submit(self, job_id: str, code: str) -> SubmitResult:
        return self.outcome

    async def close_job(self, job_id: str) -> None:
        self.closed.append(job_id)


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
    jobs: JobStore, url: str, title: str = "Backend Engineer", company: str = "Acme", country: str = "IN"
) -> str:
    posting = make_job(url=url, apply_url=url, title=title, company=company, country_codes=[country])
    return jobs.save(posting).job_id


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
        ("https://www.linkedin.com/jobs/view/1", "logged-in account"),
        ("https://in.indeed.com/viewjob?jk=1", "logged-in account"),
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
        ("https://www.linkedin.com/jobs/view/1", "www.linkedin.com", True),
        ("https://www.naukri.com/job-listings-1", "www.naukri.com", True),
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
            1, names.APPLY_RESULT, job_id, {"outcome": "needs_human", "detail": "CAPTCHA", "url": LEVER}, 1
        ),
        OWNER,
    )
    assert "needs you: CAPTCHA" in api.texts()[-1]
    assert api.sent[-1][2]["inline_keyboard"][0][0]["callback_data"] == f"applied:{job_id}"
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
