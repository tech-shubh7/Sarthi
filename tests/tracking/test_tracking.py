"""Reply tracking: triage, matching, mail parsing, the tracker task and follow-up drafts (all fictional)."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import pytest

from sarthi.ai.replies import ReplyAnswer
from sarthi.chat.tracking import followup_text, reply_text
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.db.replies import ReplyStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue import names
from sarthi.queue.tasks import RetryableError, TaskQueue
from sarthi.tracking.followup import followup_draft
from sarthi.tracking.handler import Mailbox, ReplyTracker
from sarthi.tracking.mail import Mail, MailBatch, MailError, parse
from sarthi.tracking.matcher import Applied, company_from_subject, match, normalise_company
from sarthi.tracking.triage import Kind, triage
from tests.conftest import FakeClock
from tests.matching.conftest import make_job

# ---- triage ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sender", "subject", "body", "kind", "ai"),
    [
        (
            "no-reply@greenhouse.io",
            "Thank you for applying to Acme",
            "We have received your application.",
            Kind.CONFIRMATION,
            False,
        ),
        (
            "jobs@acme.com",
            "Your application to Acme",
            "Thank you for applying. Unfortunately, we will not be moving forward.",
            Kind.REJECTION,
            False,
        ),
        ("jobs@acme.com", "Update", "After careful review we regret to inform you...", Kind.REJECTION, False),
        ("recruiter@acme.com", "Interview for Backend Engineer", "Please pick a slot.", None, True),
        (
            "recruiter@acme.com",
            "Next steps",
            "We'd like to invite you to a coding challenge on HackerRank.",
            None,
            True,
        ),
        ("jobalerts-noreply@linkedin.com", "30 new jobs for backend engineer", "", Kind.NOISE, False),
        ("info@naukri.com", "Jobs matching your profile", "", Kind.NOISE, False),
        ("hr@acme.com", "Hello", "Could you share your portfolio?", None, True),
    ],
)
def test_triage(sender: str, subject: str, body: str, kind: Kind | None, ai: bool) -> None:
    verdict = triage(sender, subject, body)
    assert (verdict.kind, verdict.needs_ai) == (kind, ai)


def test_an_interview_from_linkedin_still_reaches_the_ai() -> None:
    assert triage("inmail@linkedin.com", "Interview invitation", "").needs_ai  # interviews win over noise


# ---- matching ----------------------------------------------------------------------------------------------

APPLIED_AT = datetime(2026, 11, 10, tzinfo=UTC)
ACME = Applied(
    "j1",
    "Acme Labs Pvt. Ltd.",
    "Backend Engineer",
    "https://job-boards.greenhouse.io/acmelabs/jobs/1",
    APPLIED_AT,
)
BETA = Applied("j2", "Beta", "Data Analyst", "https://careers.beta.example/jobs/9", APPLIED_AT)
LINKEDIN_JOB = Applied("j3", "Gamma AI", "AI Engineer", "https://www.linkedin.com/jobs/view/1", APPLIED_AT)


@pytest.mark.parametrize(
    ("sender", "subject", "body", "want"),
    [
        ("no-reply@us.greenhouse-mail.io", "Thank you for applying to Acme Labs", "", "j1"),
        ("talent@acmelabs.com", "Your Backend Engineer application", "", "j1"),
        ("hr@beta.example", "Hello from Beta", "About your Data Analyst application", "j2"),
        ("careers@gammaai.com", "Gamma AI: we received your application", "", "j3"),
        ("noreply@linkedin.com", "Your application was viewed", "", None),  # the board is not the employer
        ("friend@gmail.com", "Lunch?", "", None),
    ],
)
def test_match(sender: str, subject: str, body: str, want: str | None) -> None:
    found = match(
        sender, subject, body, sent=APPLIED_AT + timedelta(days=2), applications=[ACME, BETA, LINKEDIN_JOB]
    )
    assert (found.job_id if found else None) == want


@pytest.mark.parametrize(
    ("subject", "company"),
    [
        ("Thank you for applying to Honor", "Honor"),
        ("Your application at Grafana Labs has been received", "Grafana Labs"),
        ("ESO | We received your application", "ESO"),
        ("Thank you for applying to Senior Engineer", None),  # a title, not a company
        ("Re: hello", None),
    ],
)
def test_company_from_subject(subject: str, company: str | None) -> None:
    assert company_from_subject(subject) == company


def test_normalise_company() -> None:
    assert normalise_company("Acme Labs Pvt. Ltd.") == "acme labs"
    assert normalise_company("Beta, Inc.") == "beta"


# ---- mail parsing -----------------------------------------------------------------------------------------


def raw(
    subject: str, body: str, *, html: bool = False, date_header: str = "Thu, 19 Nov 2026 10:00:00 +0530"
) -> bytes:
    message = EmailMessage()
    message["From"] = "Acme Recruiting <Talent@AcmeLabs.com>"
    message["Subject"] = subject
    message["Date"] = date_header
    message["Message-ID"] = "<abc@acmelabs.com>"
    if html:
        message.set_content(body, subtype="html")
    else:
        message.set_content(body)
    return bytes(message)


def test_parse_plain_and_html() -> None:
    mail = parse(7, raw("Interview", "Hi Asha,\n\nPick a slot."))
    assert (mail.uid, mail.sender, mail.sender_name) == (7, "talent@acmelabs.com", "Acme Recruiting")
    assert mail.sent_at == datetime(2026, 11, 19, 4, 30, tzinfo=UTC)
    assert "Pick a slot." in mail.body
    html = parse(8, raw("Hi", "<p>Hello <b>there</b></p><script>steal()</script>", html=True))
    assert html.body == "Hello there"


@pytest.mark.parametrize("header", ["not a date", ""])
def test_parse_survives_bad_dates(header: str) -> None:
    assert parse(1, raw("Hi", "x", date_header=header)).sent_at is None


def test_parse_caps_hostile_sizes() -> None:
    mail = parse(1, raw("S" * 5000, "word " * 100_000))
    assert len(mail.subject) <= 300 and len(mail.body) <= 8000


# ---- the tracker ------------------------------------------------------------------------------------------


class FakeAi:
    available = True

    def __init__(self, answer: ReplyAnswer | None) -> None:
        self.answer = answer
        self.calls = 0

    async def structured(self, prompt: Any, schema: Any, **data: Any) -> Any:
        self.calls += 1
        if self.answer is None:
            from sarthi.ai.client import AiUnavailableError  # noqa: PLC0415

            raise AiUnavailableError("busy")
        return self.answer


def mail(uid: int, sender: str, subject: str, body: str = "", message_id: str = "") -> Mail:
    return Mail(
        uid, message_id or f"<m{uid}@x>", sender, "", subject, datetime(2026, 11, 12, tzinfo=UTC), body
    )


@pytest.fixture
def tracker_parts(conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock) -> Any:
    job_id = job_store.save(
        make_job(
            url="https://job-boards.greenhouse.io/acmelabs/jobs/1",
            company="Acme Labs",
            title="Backend Engineer",
        )
    ).job_id
    apps = ApplicationStore(conn, clock=clock)
    apps.record(job_id, ApplicationStatus.SUBMITTED, site="job-boards.greenhouse.io")
    return job_id, apps


def tracker(
    conn: sqlite3.Connection,
    jobs: JobStore,
    queue: TaskQueue,
    clock: FakeClock,
    mails: list[Mail],
    ai: FakeAi,
) -> ReplyTracker:
    def fetch(*args: Any, **kwargs: Any) -> MailBatch:
        return MailBatch(1, [m for m in mails if m.uid > args[4]])

    return ReplyTracker(
        ReplyStore(conn, clock=clock),
        ApplicationStore(conn, clock=clock),
        jobs,
        ChatStore(conn, clock=clock),
        queue,
        ai=ai,  # type: ignore[arg-type]
        mailbox=Mailbox("imap.example.com", 993, "asha@example.com", "app-password"),
        name=lambda: "Asha Rao",
        fetch=fetch,
        clock=clock,
    )


async def test_replies_are_read_once_and_told_to_the_user(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock, tracker_parts: Any
) -> None:
    job_id, _ = tracker_parts
    ai = FakeAi(
        ReplyAnswer(
            kind="interview",
            confidence=0.9,
            summary="Technical interview",
            dates=["2026-12-02 15:00 IST: round 1"],
        )
    )
    mails = [
        mail(
            1,
            "no-reply@greenhouse.io",
            "Thank you for applying to Acme Labs",
            "We received your application.",
        ),
        mail(2, "talent@acmelabs.com", "Interview: Backend Engineer", "Pick a slot."),
        mail(3, "jobalerts-noreply@linkedin.com", "New jobs for you"),
        mail(4, "friend@example.com", "Interview tips?", "Saw your post"),  # unrelated: never reaches the AI
    ]
    track = tracker(conn, job_store, queue, clock, mails, ai)
    assert await track.check_mail() == 2
    assert ai.calls == 1
    notices = []
    while task := queue.claim([names.REPLY_NOTICE]):
        notices.append(task.payload)
    assert [n["kind"] for n in notices] == ["confirmation", "interview"]
    assert notices[1]["dates"] == ["2026-12-02 15:00 IST: round 1"]
    assert ReplyStore(conn).latest_kind(job_id) == "interview"
    assert await track.check_mail() == 0  # the same emails are never read twice


async def test_a_busy_ai_retries_without_losing_progress(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock, tracker_parts: Any
) -> None:
    mails = [
        mail(1, "no-reply@greenhouse.io", "Thank you for applying to Acme Labs"),
        mail(2, "talent@acmelabs.com", "Interview for Backend Engineer"),
    ]
    track = tracker(conn, job_store, queue, clock, mails, FakeAi(None))
    with pytest.raises(RetryableError):
        await track.check_mail()
    box = track.current_mailbox()
    assert box is not None
    assert track.position(box) == (1, 1)  # the first email is done; the second waits


async def test_mailbox_errors_retry(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock
) -> None:
    track = tracker(conn, job_store, queue, clock, [], FakeAi(None))

    def broken(*args: Any, **kwargs: Any) -> MailBatch:
        raise MailError("the mailbox refused the login")

    track._fetch = broken
    with pytest.raises(RetryableError, match="refused the login"):
        await track.check_mail()


async def test_no_mailbox_means_no_tracking(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock
) -> None:
    track = tracker(conn, job_store, queue, clock, [], FakeAi(None))
    track.mailbox = None
    assert await track.check_mail() == 0
    track.schedule()
    assert queue.active(names.MAIL_CHECK) == 0


async def test_follow_ups_for_quiet_applications(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock, tracker_parts: Any
) -> None:
    job_id, apps = tracker_parts
    track = tracker(conn, job_store, queue, clock, [], FakeAi(None))
    assert track.check_quiet() == 0  # just applied
    clock.advance(11 * 86400)
    assert track.check_quiet() == 1
    task = queue.claim([names.FOLLOWUP_NOTICE])
    assert task is not None
    assert "Asha Rao" in task.payload["draft"] and "Backend Engineer" in task.payload["draft"]
    assert track.check_quiet() == 0  # not again so soon
    clock.advance(11 * 86400)
    assert track.check_quiet() == 1  # a second reminder
    clock.advance(11 * 86400)
    assert track.check_quiet() == 0  # at most two
    assert apps.reminders(job_id)[0] == 2


async def test_no_follow_up_once_the_employer_wrote(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock, tracker_parts: Any
) -> None:
    track = tracker(
        conn,
        job_store,
        queue,
        clock,
        [mail(1, "no-reply@greenhouse.io", "Thank you for applying to Acme Labs")],
        FakeAi(None),
    )
    await track.check_mail()
    clock.advance(11 * 86400)
    assert track.check_quiet() == 0


async def test_tasks_reschedule_themselves(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock
) -> None:
    track = tracker(conn, job_store, queue, clock, [], FakeAi(None))
    await track.handle_mail(TaskRecord(1, names.MAIL_CHECK, "a", {}, 1))
    await track.handle_quiet(TaskRecord(1, names.QUIET_CHECK, "a", {}, 1))
    assert queue.active(names.MAIL_CHECK) == 1
    assert queue.active(names.QUIET_CHECK) == 1


# ---- texts ------------------------------------------------------------------------------------------------


def test_followup_draft_only_uses_the_users_facts() -> None:
    draft = followup_draft("Asha Rao", "Backend Engineer", "Acme Labs", date(2026, 11, 5))
    assert "5 November 2026" in draft
    assert draft.endswith("Best regards,\nAsha Rao")
    assert followup_draft("", "Dev", "Beta", date(2026, 1, 1)).endswith("Best regards")


def test_messages_escape_everything() -> None:
    job = make_job(title="<b>Dev</b>", company="A & B")
    text = reply_text(
        job,
        {"kind": "interview", "subject": "<script>", "summary": "x", "dates": ["<i>"], "actions": ["Reply"]},
    )
    assert "&lt;b&gt;Dev&lt;/b&gt;" in text and "A &amp; B" in text and "&lt;script&gt;" in text
    assert text.startswith("🎉 Interview invitation")
    assert "&lt;/pre&gt;" in followup_text(job, {"draft": "</pre><b>x", "days": 10})
    assert "Not selected" in reply_text(None, {"kind": "rejection"})
