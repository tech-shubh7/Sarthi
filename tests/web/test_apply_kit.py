"""The quick-apply kit: open the job, attach, copy, done; only data inside Sarthi's own folder."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from sarthi.db.answers import AnswerStore
from sarthi.db.documents import DocumentRecord, DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.profile.master import Contact, MasterResume, master_path
from sarthi.web.app import create_app, read_only
from sarthi.web.apply_kit import apply_kit
from tests.conftest import FakeClock
from tests.matching.conftest import make_job


def setup(conn: sqlite3.Connection, job_store: JobStore, tmp_path: Path) -> str:
    job_id = job_store.save(
        make_job(url="https://in.indeed.example/viewjob?jk=1", company="Example Labs")
    ).job_id
    folder = tmp_path / "documents" / job_id
    folder.mkdir(parents=True)
    (folder / "Asha_Resume.pdf").write_bytes(b"%PDF-1.4 resume")
    (folder / "Asha_Cover_Letter.pdf").write_bytes(b"%PDF-1.4 letter")
    (folder / "cover_letter.txt").write_text(
        "Dear Example Labs hiring team,\n\nI built 12 services.", encoding="utf-8"
    )
    DocumentStore(conn).save(
        DocumentRecord(
            job_id, "v", f"documents/{job_id}/Asha_Resume.pdf", f"documents/{job_id}/Asha_Cover_Letter.pdf"
        )
    )
    master = MasterResume(
        name="Asha Rao",
        contact=Contact(
            email="asha@example.com",
            phone="+91 90000 00000",
            location="Pune, India",
            links=("https://www.linkedin.com/in/asha-demo", "https://asha.example.dev"),
        ),
    )
    master_path(tmp_path).write_text(master.model_dump_json(), encoding="utf-8")
    AnswerStore(conn).save("Notice period", "30 days")
    return job_id


def test_the_kit_has_the_job_documents_details_and_answers(
    conn: sqlite3.Connection, job_store: JobStore, tmp_path: Path
) -> None:
    job_id = setup(conn, job_store, tmp_path)
    kit = apply_kit(conn, tmp_path, job_id, None)
    assert kit is not None
    assert kit.url == "https://in.indeed.example/viewjob?jk=1" and kit.resume and kit.letter
    assert kit.letter_text.startswith("Dear Example Labs hiring team,")
    values = {f.label: f.value for f in kit.details}
    assert values["Full name"] == "Asha Rao" and values["LinkedIn"].endswith("asha-demo")
    assert values["Portfolio / website"] == "https://asha.example.dev"
    assert [(a.label, a.value) for a in kit.answers] == [("Notice period", "30 days")]
    assert apply_kit(conn, tmp_path, "no-such-job", None) is None


def test_the_kit_page_renders_and_unknown_jobs_are_404(
    conn: sqlite3.Connection, job_store: JobStore, tmp_path: Path, clock: FakeClock
) -> None:
    job_id = setup(conn, job_store, tmp_path)
    client = TestClient(
        create_app(read_only(tmp_path / "data" / "sarthi.db"), clock=clock, data_dir=tmp_path),
        base_url="http://127.0.0.1",
    )
    page = client.get(f"/applications/{job_id}/apply-kit")
    assert page.status_code == 200
    html = page.text
    assert "Open the job on the site" in html and 'target="_blank"' in html
    assert f"/applications/{job_id}/resume.pdf" in html and "asha@example.com" in html
    assert "Notice period" in html and "Dear Example Labs hiring team," in html
    assert "<script" not in html and 'style="' not in html
    assert client.get("/applications/no-such-job/apply-kit").status_code == 404
