"""Form questions before the browser: reading them, the applicant's facts, the answer bank and the chat flow."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from sarthi.answers.handler import FormChecker
from sarthi.answers.questions import (
    Posting,
    ashby_fields,
    greenhouse_fields,
    lever_fields,
    posting_for,
    read_questions,
)
from sarthi.chat.questions import ASKING_KEY, QuestionFlow, fitted
from sarthi.db.answers import AnswerStore
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.jobs.forms import FormField
from sarthi.profile.applicant import applicant, relocation, salary_text, visa_facts
from sarthi.profile.master import Contact, Job, MasterResume, School, save_master
from sarthi.profile.models import Preferences, Profile
from sarthi.queue import names
from sarthi.queue.tasks import TaskQueue
from tests.chat.test_bot import FakeApi
from tests.conftest import FakeClock
from tests.matching.conftest import make_job

# ---- which form a job uses ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("urls", "want"),
    [
        (("https://job-boards.greenhouse.io/acme/jobs/123",), Posting("greenhouse", "acme", "123")),
        (("https://boards.greenhouse.io/acme/jobs/123?gh_src=x",), Posting("greenhouse", "acme", "123")),
        (("https://jobs.lever.co/acme/1a2b-3c/apply",), Posting("lever", "acme", "1a2b-3c")),
        (("https://jobs.ashbyhq.com/acme/9f8e/application?utm=x",), Posting("ashby", "acme", "9f8e")),
        (
            ("https://in.indeed.com/viewjob?jk=1", "https://jobs.lever.co/acme/7"),
            Posting("lever", "acme", "7"),
        ),
        (("https://jobs.lever.co/acme",), None),
        (("https://evil.com/jobs.lever.co/acme/1",), None),
        (("https://jobs.lever.co.evil.com/acme/1",), None),
        (("https://job-boards.greenhouse.io/acme/jobs/notanumber",), None),
        (("https://jobs.ashbyhq.com/..%2F..%2Fetc/passwd",), None),
        (("", "not a url"), None),
    ],
)
def test_posting_for(urls: tuple[str, ...], want: Posting | None) -> None:
    assert posting_for(*urls) == want


# ---- reading each hiring system's questions -------------------------------------------------------------

GREENHOUSE = {
    "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
        {
            "label": "Resume/CV",
            "required": True,
            "fields": [{"name": "resume_text", "type": "textarea"}, {"name": "resume", "type": "input_file"}],
        },
        {
            "label": "Will you require sponsorship?",
            "required": True,
            "fields": [
                {
                    "name": "question_1",
                    "type": "multi_value_single_select",
                    "values": [{"label": "Yes"}, {"label": "No"}],
                }
            ],
        },
        {"label": "Lat", "fields": [{"name": "latitude", "type": "input_hidden"}]},
        {"label": "", "fields": [{"name": "x", "type": "input_text"}]},
        "junk",
    ]
}


def test_greenhouse_fields() -> None:
    fields = greenhouse_fields(GREENHOUSE)
    assert [(f.key, f.type, f.required) for f in fields] == [
        ("first_name", "text", True),
        ("resume", "file", True),
        ("question_1", "select", True),
    ]
    assert fields[2].options == ("Yes", "No")


def test_ashby_fields() -> None:
    data = {
        "data": {
            "jobPosting": {
                "applicationForm": {
                    "sections": [
                        {
                            "fieldEntries": [
                                {
                                    "isRequired": True,
                                    "field": {"path": "_systemfield_name", "title": "Name", "type": "String"},
                                },
                                {
                                    "isRequired": True,
                                    "field": {"path": "auth", "title": "Authorized?", "type": "Boolean"},
                                },
                                {
                                    "isRequired": False,
                                    "field": {
                                        "path": "old",
                                        "title": "Old",
                                        "type": "String",
                                        "isDeactivated": True,
                                    },
                                },
                                {
                                    "isRequired": False,
                                    "field": {
                                        "path": "src",
                                        "title": "Source",
                                        "type": "ValueSelect",
                                        "selectableValues": [
                                            {"label": "LinkedIn"},
                                            {"label": "Gone", "isArchived": True},
                                        ],
                                    },
                                },
                            ]
                        }
                    ]
                }
            }
        }
    }
    fields = ashby_fields(data)
    assert [(f.key, f.type, f.options) for f in fields] == [
        ("_systemfield_name", "text", ()),
        ("auth", "select", ("Yes", "No")),
        ("src", "select", ("LinkedIn",)),
    ]
    assert ashby_fields({"data": None}) == []


LEVER = """<html><form id="application-form">
<li class="application-question"><div class="application-label">Full name<span class="required">✱</span></div>
<div class="application-field"><input name="name" type="text" required></div></li>
<li class="application-question"><div class="application-label">Gender</div>
<div class="application-field"><input name="eeo[gender]" type="radio" value="Female"></div></li>
<li class="application-question"><div class="application-label">Notice &amp; start?</div>
<div class="application-field"><select name="cards[a][field0]"><option>Now</option><option>Later</option></select></div></li>
<input type="hidden" name="secret" value="x">
</form></html>"""


def test_lever_fields() -> None:
    fields = lever_fields(LEVER)
    assert [(f.key, f.label, f.type, f.required, f.options) for f in fields] == [
        ("name", "Full name", "text", True, ()),
        ("cards[a][field0]", "Notice & start?", "select", False, ("Now", "Later")),
    ]
    assert lever_fields("<html>no form</html>") == []
    unknown_layout = (
        '<form id="application-form"><li class="application-question"><input name="q"></li></form>'
    )
    assert lever_fields(unknown_layout) == []  # a question without a label: give up, never mislabel


def mock(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_read_questions_per_system() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "greenhouse" in request.url.host:
            assert request.url.params["questions"] == "true"
            return httpx.Response(200, json=GREENHOUSE)
        if "ashby" in request.url.host:
            body = json.loads(request.content)
            assert body["variables"] == {"organizationHostedJobsPageName": "acme", "jobPostingId": "9"}
            return httpx.Response(200, json={"data": {"jobPosting": None}})
        return httpx.Response(200, text=LEVER)

    assert len(await read_questions(Posting("greenhouse", "acme", "1"), mock(handler)) or []) == 3
    assert await read_questions(Posting("ashby", "acme", "9"), mock(handler)) is None  # nothing readable
    assert len(await read_questions(Posting("lever", "acme", "x"), mock(handler)) or []) == 2
    assert await read_questions(Posting("workday", "acme", "x"), mock(handler)) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        httpx.Response(200, content=b"x" * 3_000_001),
    ],
)
async def test_failures_never_raise(response: httpx.Response) -> None:
    assert await read_questions(Posting("greenhouse", "acme", "1"), mock(lambda r: response)) is None


async def test_network_errors_never_raise() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    assert await read_questions(Posting("lever", "acme", "1"), mock(fail)) is None


# ---- the applicant's facts, per job ----------------------------------------------------------------------


def profile(**prefs: Any) -> Profile:
    data = {"roles": ("Backend Engineer",), "home_country": "IN", "countries": frozenset({"AE"})} | prefs
    return Profile(preferences=Preferences.model_validate(data), resume_text="x")


@pytest.mark.parametrize(
    ("job", "facts"),
    [
        ({"country_codes": ["IN"]}, (True, False)),
        ({"country_codes": ["US"]}, (False, True)),
        ({"country_codes": [], "remote_type": "remote", "location_text": ""}, (True, False)),  # anywhere
        ({"country_codes": [], "remote_type": "unknown"}, (None, None)),  # unknown: ask
    ],
)
def test_visa_facts_follow_the_job_country(
    job: dict[str, Any], facts: tuple[bool | None, bool | None]
) -> None:
    assert visa_facts(profile(), make_job(**job)) == facts


def test_relocation_and_salary() -> None:
    assert relocation(profile(), make_job(country_codes=["AE"])) is True
    assert relocation(profile(), make_job(country_codes=["DE"])) is False
    assert relocation(profile(), make_job(country_codes=[], remote_type="unknown")) is None
    floor = {"currency": "INR", "minimum": 1200000, "period": "year"}
    assert salary_text(profile(min_salary=floor)) == "1200000 INR per year"
    assert salary_text(profile()) == ""


def test_applicant_from_profile_and_resume() -> None:
    master = MasterResume(
        name="Asha Rao",
        contact=Contact(
            email="asha@example.com",
            phone="+91 98765 43210",
            links=("linkedin.com/in/asha-example", "https://asha.example.com"),
        ),
        experience=(Job(company="Acme Labs", title="Software Engineer", start="2025", end="Present"),),
        education=(School(school="Pune University", degree="B.E.", start="2020", end="Jun 2024"),),
    )
    who = applicant(profile(), master, make_job(country_codes=["IN"]), {"Notice period": "30 days"})
    assert (who.first_name, who.last_name, who.country, who.location) == ("Asha", "Rao", "India", "India")
    assert who.linkedin == "https://linkedin.com/in/asha-example"
    assert who.website == "https://asha.example.com"
    assert (who.current_company, who.current_title) == ("Acme Labs", "Software Engineer")
    assert (who.school, who.start_year, who.end_year) == ("Pune University", "2020", "2024")
    assert who.saved_answers == {"Notice period": "30 days"}
    past = master.model_copy(update={"experience": (Job(company="Old Co", title="Dev", end="2024"),)})
    assert applicant(profile(), past, make_job(), {}).current_company == ""  # not a current job
    assert applicant(profile(), None, make_job(), {}).first_name == ""


# ---- the answer bank -------------------------------------------------------------------------------------


@pytest.fixture
def answers(conn: sqlite3.Connection, clock: FakeClock) -> AnswerStore:
    return AnswerStore(conn, clock=clock)


def test_answer_bank(answers: AnswerStore, job_store: JobStore) -> None:
    answers.save("Notice period", "30 days")
    answers.save("Notice period", "  15 days ")
    answers.save("", "ignored")
    answers.save("Blank", "   ")
    assert answers.saved() == {"Notice period": "15 days"}
    answers.save("Long", "x" * 5000)
    assert len(answers.saved()["Long"]) == 2000
    assert answers.forget("Long")
    assert not answers.forget("Long")


def test_pending_questions(answers: AnswerStore, job_store: JobStore) -> None:
    job_id = job_store.save(make_job()).job_id
    q1 = FormField("q1", "Why us?", "textarea", True)
    q2 = FormField("q2", "Relocate?", "select", True, ("Yes", "No"))
    answers.set_pending(job_id, [q1, q2, q1])  # a repeated key is kept once
    assert answers.pending(job_id) == [q1, q2]
    answers.answered(job_id, "q1")
    assert answers.pending(job_id) == [q2]
    answers.clear(job_id)
    assert answers.pending(job_id) == []


# ---- the form check -------------------------------------------------------------------------------------


def lever_job(job_store: JobStore) -> str:
    return job_store.save(
        make_job(apply_url="https://jobs.lever.co/acme/abc-1/apply", country_codes=["IN"])
    ).job_id


def checker(
    job_store: JobStore, conn: sqlite3.Connection, queue: TaskQueue, tmp_path: Path, page: str
) -> FormChecker:
    transport = mock(lambda _: httpx.Response(200, text=page))
    return FormChecker(
        job_store,
        DocumentStore(conn),
        AnswerStore(conn),
        queue,
        profile,
        data_dir=tmp_path,
        transport=transport,
    )


async def test_unknown_required_questions_are_asked_first(
    job_store: JobStore, conn: sqlite3.Connection, queue: TaskQueue, tmp_path: Path
) -> None:
    page = LEVER.replace("Full name", "Why do you want to join us?")
    save_master(tmp_path / "master_resume.json", MasterResume(name="Asha Rao"))
    job_id = lever_job(job_store)
    await checker(job_store, conn, queue, tmp_path, page).handle(
        TaskRecord(1, names.FORM_CHECK, job_id, {}, 1)
    )
    assert [q.label for q in AnswerStore(conn).pending(job_id)] == ["Why do you want to join us?"]
    assert queue.active(names.ASK_QUESTIONS) == 1
    assert queue.active(names.APPLY) == 0


async def test_known_answers_go_straight_to_applying(
    job_store: JobStore, conn: sqlite3.Connection, queue: TaskQueue, tmp_path: Path
) -> None:
    save_master(tmp_path / "master_resume.json", MasterResume(name="Asha Rao"))
    job_id = lever_job(job_store)
    await checker(job_store, conn, queue, tmp_path, LEVER).handle(
        TaskRecord(1, names.FORM_CHECK, job_id, {}, 1)
    )
    assert queue.active(names.APPLY) == 1


async def test_unreadable_forms_go_to_the_browser(
    job_store: JobStore, conn: sqlite3.Connection, queue: TaskQueue, tmp_path: Path
) -> None:
    job_id = job_store.save(make_job(apply_url="https://careers.example.com/apply/1")).job_id
    await checker(job_store, conn, queue, tmp_path, "").handle(TaskRecord(1, names.FORM_CHECK, job_id, {}, 1))
    assert queue.active(names.APPLY) == 1


# ---- asking in the chat ---------------------------------------------------------------------------------

OWNER = 111


@pytest.fixture
def flow(conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock) -> QuestionFlow:
    return QuestionFlow(
        FakeApi(), ChatStore(conn, clock=clock), job_store, AnswerStore(conn, clock=clock), queue
    )  # type: ignore[arg-type]


async def test_questions_are_asked_one_by_one_and_remembered(
    flow: QuestionFlow, job_store: JobStore, queue: TaskQueue
) -> None:
    job_id = job_store.save(make_job()).job_id
    flow.answers.set_pending(
        job_id,
        [
            FormField("q1", "Relocate to Dubai?", "select", True, ("Yes", "No")),
            FormField("q2", "Why us?", "textarea", True),
        ],
    )
    api: Any = flow.api
    await flow.handle(TaskRecord(1, names.ASK_QUESTIONS, job_id, {}, 1), OWNER)
    first = api.sent[-1]
    assert "Relocate to Dubai?" in first[1]
    assert [b["callback_data"] for row in first[2]["inline_keyboard"] for b in row] == [
        "ans:0",
        "ans:1",
        "ans:cancel",
    ]
    await flow.on_button(OWNER, "ans:0")
    assert "Why us?" in api.sent[-1][1]
    assert await flow.on_text(OWNER, "  ")  # blank: asked again
    assert "Please type an answer." in api.sent[-1][1]
    assert await flow.on_text(OWNER, "I like the product\x00")
    assert flow.answers.saved() == {"Relocate to Dubai?": "Yes", "Why us?": "I like the product"}
    assert queue.active(names.APPLY) == 1
    assert flow.waiting() is None
    assert not await flow.on_text(OWNER, "random chat")  # nothing waiting: normal conversation


async def test_cancel_skips_the_job(flow: QuestionFlow, job_store: JobStore, queue: TaskQueue) -> None:
    job_id = job_store.save(make_job()).job_id
    flow.answers.set_pending(job_id, [FormField("q1", "Why us?", "textarea", True)])
    await flow.handle(TaskRecord(1, names.ASK_QUESTIONS, job_id, {}, 1), OWNER)
    await flow.on_button(OWNER, "ans:cancel")
    assert flow.chats.job_status(job_id) is JobStatus.SKIPPED
    assert flow.answers.pending(job_id) == []
    assert queue.active(names.APPLY) == 0


@pytest.mark.parametrize("data", ["ans:9", "ans:-1", "ans:x", "ans:"])
async def test_stale_or_hostile_buttons_do_nothing(
    flow: QuestionFlow, job_store: JobStore, data: str
) -> None:
    job_id = job_store.save(make_job()).job_id
    flow.answers.set_pending(job_id, [FormField("q1", "Relocate?", "select", True, ("Yes", "No"))])
    await flow.handle(TaskRecord(1, names.ASK_QUESTIONS, job_id, {}, 1), OWNER)
    await flow.on_button(OWNER, data)
    assert flow.answers.saved() == {}


@pytest.mark.parametrize("raw", ["not json", '{"job": 1}', "[]", '{"job": "x"}'])
def test_a_corrupt_waiting_state_is_ignored(flow: QuestionFlow, raw: str) -> None:
    flow.chats.set_state(ASKING_KEY, raw)
    assert flow.waiting() is None


@pytest.mark.parametrize(
    ("question", "text", "answer"),
    [
        (FormField("k", "Start?", "date", True), "2 weeks", "2 weeks"),
        (FormField("k", "Start?", "date", True), "whenever", None),
        (FormField("k", "Relocate?", "select", True, ("Yes", "No")), "yes please", "Yes"),
        (FormField("k", "Relocate?", "select", True, ("Yes", "No")), "maybe", None),
        (FormField("k", "Stack?", "checkbox", True, ("Python", "Go", "Rust")), "python and go", "Python, Go"),
        (FormField("k", "Why?", "textarea", True), "\u202eBecause", "Because"),
    ],
)
def test_answers_must_fit_the_question(question: FormField, text: str, answer: str | None) -> None:
    assert fitted(question, text)[0] == answer
