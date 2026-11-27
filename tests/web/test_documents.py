"""The tailored resume and cover letter each application used, shown and served by the dashboard."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.dashboard import DashboardQueries
from sarthi.db.documents import DocumentRecord, DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.web.app import create_app, read_only
from tests.conftest import FakeClock
from tests.matching.conftest import make_job

RESUME = b"%PDF-1.7 fictional resume"
LETTER = b"%PDF-1.7 fictional letter"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(conn: sqlite3.Connection, data_dir: Path, clock: FakeClock) -> TestClient:
    # The read-only dashboard (no actions): documents must still be viewable.
    app = create_app(read_only(data_dir / "sarthi.db"), clock=clock, data_dir=data_dir)
    return TestClient(app, base_url="http://127.0.0.1")


def applied(conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock, url: str) -> str:
    # A distinct company per job: the same title at the same company is one job (deduplicated).
    job_id = job_store.save(make_job(url=url, title="AI Engineer", company=f"Acme {url[-1]}")).job_id
    ApplicationStore(conn, clock=clock).record(job_id, ApplicationStatus.SUBMITTED, site="greenhouse")
    return job_id


def made(conn: sqlite3.Connection, job_id: str, resume: str, letter: str) -> None:
    DocumentStore(conn).save(DocumentRecord(job_id, "1", resume, letter))


def write_docs(data_dir: Path, job_id: str) -> tuple[str, str]:
    folder = data_dir / "documents" / job_id
    folder.mkdir(parents=True)
    (folder / "Aarav_Mehta_Acme_Resume.pdf").write_bytes(RESUME)
    (folder / "Aarav_Mehta_Acme_Cover_Letter.pdf").write_bytes(LETTER)
    return (
        f"documents/{job_id}/Aarav_Mehta_Acme_Resume.pdf",
        f"documents/{job_id}/Aarav_Mehta_Acme_Cover_Letter.pdf",
    )


def test_each_application_links_and_serves_its_own_documents(
    client: TestClient, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock, data_dir: Path
) -> None:
    with_docs = applied(conn, job_store, clock, "https://jobs.example/1")
    without = applied(conn, job_store, clock, "https://jobs.example/2")
    made(conn, with_docs, *write_docs(data_dir, with_docs))

    page = client.get("/applications/submitted").text
    assert f'href="/applications/{with_docs}/resume.pdf" target="_blank" rel="noopener noreferrer"' in page
    assert f'href="/applications/{with_docs}/cover-letter.pdf"' in page
    assert f"/applications/{without}/resume.pdf" not in page
    assert f"/applications/{without}/cover-letter.pdf" not in page

    resume = client.get(f"/applications/{with_docs}/resume.pdf")
    assert resume.status_code == 200 and resume.content == RESUME
    assert resume.headers["content-type"] == "application/pdf"
    assert resume.headers["content-disposition"] == 'inline; filename="Aarav_Mehta_Acme_Resume.pdf"'
    assert "Content-Security-Policy" in resume.headers
    letter = client.get(f"/applications/{with_docs}/cover-letter.pdf")
    assert letter.status_code == 200 and letter.content == LETTER

    assert client.get(f"/applications/{without}/resume.pdf").status_code == 404
    assert client.get("/applications/nope/cover-letter.pdf").status_code == 404


def test_only_pdfs_inside_the_data_folder_are_served(
    client: TestClient, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock, data_dir: Path
) -> None:
    data_dir.mkdir(exist_ok=True)
    (data_dir.parent / "secret.pdf").write_bytes(b"%PDF secret")
    (data_dir / "profile.json").write_text("{}")
    outside = applied(conn, job_store, clock, "https://jobs.example/1")
    made(conn, outside, "../secret.pdf", str(data_dir.parent / "secret.pdf"))
    not_pdf = applied(conn, job_store, clock, "https://jobs.example/2")
    made(conn, not_pdf, "profile.json", "profile.json")
    missing = applied(conn, job_store, clock, "https://jobs.example/3")
    made(conn, missing, "documents/x/gone.pdf", "documents/x/gone.pdf")

    for job_id in (outside, not_pdf, missing):
        assert client.get(f"/applications/{job_id}/resume.pdf").status_code == 404
        assert client.get(f"/applications/{job_id}/cover-letter.pdf").status_code == 404
    assert client.get("/applications/..%2F..%2Fsecret/resume.pdf").status_code == 404


def test_nothing_is_served_without_a_data_folder(
    conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock, data_dir: Path
) -> None:
    job_id = applied(conn, job_store, clock, "https://jobs.example/1")
    made(conn, job_id, *write_docs(data_dir, job_id))
    app = create_app(read_only(data_dir / "sarthi.db"), clock=clock)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get(f"/applications/{job_id}/resume.pdf").status_code == 404


def test_rows_say_which_documents_exist_in_one_query(
    conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock, data_dir: Path
) -> None:
    with_docs = applied(conn, job_store, clock, "https://jobs.example/1")
    without = applied(conn, job_store, clock, "https://jobs.example/2")
    made(conn, with_docs, "documents/a/R.pdf", "documents/a/L.pdf")
    queries = DashboardQueries(conn)
    rows = {r.job_id: r for r in queries.applications()}
    assert (rows[with_docs].resume, rows[with_docs].letter) == (True, True)
    assert (rows[without].resume, rows[without].letter) == (False, False)
    waiting = {r.job_id: r for r in queries.applications_for([with_docs, without])}
    assert (waiting[with_docs].resume, waiting[without].letter) == (True, False)
