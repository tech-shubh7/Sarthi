"""The browser extension's service: pairing, finding the job, the answers, the files and the package."""

import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from sarthi.db.answers import AnswerStore
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.chat import ChatStore
from sarthi.db.documents import DocumentRecord, DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.extension import CODE_LIFE, ExtensionService
from sarthi.jobs.forms import FormField
from sarthi.profile.models import Preferences, Profile
from sarthi.queue import names
from sarthi.queue.tasks import TaskQueue
from tests.conftest import FakeClock
from tests.matching.conftest import make_job

PROFILE = Profile(
    preferences=Preferences(roles=("Backend Engineer",), home_country="IN", years_experience=2),
    skills=("Python", "FastAPI"),
    resume_text="Asha Rao (fictional). Python, FastAPI.",
)
GREENHOUSE = "https://boards.greenhouse.io/acme/jobs/42"


@pytest.fixture
def service(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock, tmp_path: Path
) -> ExtensionService:
    marked: list[str] = []
    made = ExtensionService(
        state=ChatStore(conn, clock=clock),
        jobs=job_store,
        documents=DocumentStore(conn, clock=clock),
        answers=AnswerStore(conn, clock=clock),
        applications=ApplicationStore(conn, clock=clock),
        queue=queue,
        load_profile=lambda: PROFILE,
        mark_applied=lambda job_id: False,  # no Telegram flow: the service records it itself
        data_dir=tmp_path,
        clock=clock,
    )
    made.marked = marked  # type: ignore[attr-defined]
    return made


def test_pairing_is_one_code_once_and_ends_the_older_key(service: ExtensionService, clock: FakeClock) -> None:
    assert service.pair("ANYTHING") is None  # no code shown yet
    code = service.code()
    assert len(code) == 8 and not set(code) & set("01OIL")
    assert service.pair("WRONG123") is None
    first = service.pair(code.lower())  # typed in small letters: still the code
    assert first and service.allowed(first)
    assert service.pair(code) is None  # used up
    second = service.pair(service.code())
    assert second and service.allowed(second) and not service.allowed(first)
    assert not service.allowed("") and not service.allowed("guess")


def test_an_old_code_expires(service: ExtensionService, clock: FakeClock) -> None:
    code = service.code()
    clock.advance(CODE_LIFE.total_seconds() + 1)
    assert service.pair(code) is None


def test_the_open_page_finds_its_job_and_the_waiting_ones_follow(
    service: ExtensionService, job_store: JobStore, conn: sqlite3.Connection, clock: FakeClock
) -> None:
    here = job_store.save(make_job(url=GREENHOUSE, apply_url=GREENHOUSE, title="Backend Engineer")).job_id
    board = "https://in.indeed.com/viewjob?jk=abc"
    employer = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Pune/Python-Developer_R1"
    other = job_store.save(
        make_job(url=board, apply_url=employer, title="Python Developer", company="Beta")
    ).job_id
    ApplicationStore(conn, clock=clock).record(other, ApplicationStatus.NEEDS_HUMAN, site="in.indeed.com")
    found = service.jobs_for(f"{GREENHOUSE}/?utm_source=x")  # tracking parameters do not matter
    assert found.matched == here
    assert [j.job_id for j in found.jobs] == [here, other]
    assert found.jobs[1].note == "Apply yourself"
    assert service.jobs_for(f"{employer}/apply").matched == other  # the employer's own form, one step on
    assert service.jobs_for("https://example.com/").matched is None
    assert service.jobs_for("not a url").matched is None


def test_fill_answers_from_the_profile_and_starts_missing_documents(
    service: ExtensionService, job_store: JobStore, queue: TaskQueue
) -> None:
    job_id = job_store.save(make_job(url=GREENHOUSE)).job_id
    fields = [
        FormField("a", "How many years of work experience do you have with Python?", "text", True),
        FormField("b", "Why do you want to work here?", "textarea", True),
        FormField("c", "Resume/CV", "file", True),
    ]
    result = service.fill(job_id, fields)
    assert result is not None
    assert result.answers["a"].value == "2" and result.answers["a"].source == "profile"
    assert result.missing == ["b"]
    assert result.resume == "" and "c" not in result.answers  # no tailored resume yet...
    task = queue.claim([names.DOCUMENTS])
    assert task is not None and task.key == job_id  # ...so it is being made
    assert service.fill("unknown-job", fields) is None


def test_files_are_only_this_jobs_pdfs_inside_the_data_folder(
    service: ExtensionService, conn: sqlite3.Connection, job_store: JobStore, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = job_store.save(make_job(url=GREENHOUSE)).job_id
    folder = tmp_path / "documents" / job_id
    folder.mkdir(parents=True)
    (folder / "Asha_Resume.pdf").write_bytes(b"%PDF-1.4")
    store = DocumentStore(conn, clock=clock)
    store.save(
        DocumentRecord(job_id, PROFILE.version, f"documents/{job_id}/Asha_Resume.pdf", "../../etc/passwd")
    )
    assert service.file(job_id, "resume") == (folder / "Asha_Resume.pdf").resolve()
    assert service.file(job_id, "cover") is None  # outside the data folder: never served
    assert service.file("other", "resume") is None


def test_applied_is_recorded_and_new_answers_kept_but_never_secrets(
    service: ExtensionService, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    job_id = job_store.save(make_job(url=GREENHOUSE)).job_id
    given = [
        ("What is your notice period?", "30 days"),
        ("Why do you want to work here?", "Your payments work."),  # this employer only
        ("Create a password", "hunter2"),
        ("Enter the one-time code", "123456"),
        ("PAN number", "ABCDE1234F"),
        ("", "orphan"),
    ]
    assert service.applied(job_id, given)
    saved = AnswerStore(conn, clock=clock).saved()
    assert saved["What is your notice period?"] == "30 days"
    assert not any("password" in q.lower() or "one-time" in q or "PAN" in q for q in saved)
    assert "Why do you want to work here?" not in saved  # kept for this job only
    assert any(q.startswith("Why do you want to work here?") and job_id in q for q in saved)
    record = ApplicationStore(conn, clock=clock).get(job_id)
    assert record is not None and record.status is ApplicationStatus.APPLIED_BY_USER
    assert not service.applied("unknown-job", given)


def test_the_package_is_a_loadable_extension() -> None:
    zipped = zipfile.ZipFile(io.BytesIO(ExtensionService.package()))
    names_in = set(zipped.namelist())
    assert {"manifest.json", "background.js", "fill.js", "popup.html", "popup.js", "scan.js"} <= names_in
    assert {"icon-32.png", "icon-128.png"} <= names_in
    manifest = json.loads(zipped.read("manifest.json"))
    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) == {"activeTab", "scripting", "storage"}  # nothing broader
    assert manifest["host_permissions"] == ["http://127.0.0.1/*", "http://localhost/*"]
    assert zipped.read("scan.js").decode().count("self.__sarthiScan = ") == 1
