"""The browser extension's API: only the paired extension gets in, and what it sends is checked."""

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sarthi.jobs.forms import FormField
from sarthi.profile.assist import Answer, AssistFill
from sarthi.web.actions import ExtensionJob, ExtensionJobs
from sarthi.web.app import create_app, read_only
from sarthi.web.extension_api import PAIR_TRIES
from tests.conftest import FakeClock
from tests.web.test_settings import HERE, FakeActions

EXT = {"origin": "chrome-extension://abcdefghijklmnop"}
KEY = "the-paired-key"


class ExtensionActions(FakeActions):
    def __init__(self, pdf: Path) -> None:
        super().__init__()
        self.pdf = pdf
        self.codes: list[str] = []
        self.filled: list[tuple[str, list[FormField]]] = []
        self.applied_with: list[tuple[str, list[tuple[str, str]]]] = []

    def extension_code(self) -> str:
        self.codes.append("ABCD2345")
        return "ABCD2345"

    def pair_extension(self, code: str) -> str | None:
        return KEY if self.codes and code == "ABCD2345" else None

    def extension_allowed(self, key: str) -> bool:
        return key == KEY

    def extension_jobs(self, url: str) -> ExtensionJobs:
        return ExtensionJobs("j1", [ExtensionJob("j1", "Backend Engineer", "Acme", "Apply yourself")])

    def extension_scan_config(self, url: str) -> dict[str, str]:
        return {"root": "form", "container": ".field"}

    def extension_fill(self, job_id: str, fields: list[FormField]) -> AssistFill | None:
        self.filled.append((job_id, fields))
        if job_id != "j1":
            return None
        return AssistFill(
            {"s-1": Answer("Asha", "profile"), "s-2": Answer("resume", "file")}, ["s-3"], "r.pdf"
        )

    def extension_file(self, job_id: str, kind: str) -> Path | None:
        return self.pdf if job_id == "j1" and kind == "resume" else None

    def extension_applied(self, job_id: str, given: list[tuple[str, str]]) -> bool:
        self.applied_with.append((job_id, given))
        return job_id == "j1"

    def extension_package(self) -> bytes:
        return b"PK\x03\x04zip"


@pytest.fixture
def actions(tmp_path: Path) -> ExtensionActions:
    pdf = tmp_path / "Asha_Rao_Acme_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 resume")
    return ExtensionActions(pdf)


@pytest.fixture
def client(
    conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock, actions: ExtensionActions
) -> TestClient:
    app = create_app(
        read_only(tmp_path / "data" / "sarthi.db"), clock=clock, actions=actions, data_dir=tmp_path
    )
    return TestClient(app, base_url="http://127.0.0.1")


def paired() -> dict[str, str]:
    return EXT | {"authorization": f"Bearer {KEY}"}


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 403),  # no origin
        ({"origin": "https://evil.example", "authorization": f"Bearer {KEY}"}, 403),  # a website
        ({"origin": "http://127.0.0.1", "authorization": f"Bearer {KEY}"}, 403),  # the dashboard's own pages
        (EXT, 401),  # an extension without the key
        (EXT | {"authorization": "Bearer wrong"}, 401),
        (EXT | {"authorization": KEY}, 401),  # not a Bearer key
    ],
)
def test_only_the_paired_extension_gets_in(client: TestClient, headers: dict[str, str], status: int) -> None:
    for path in ("/ext/jobs?url=x", "/ext/scan-config?url=x", "/ext/file/j1/resume"):
        assert client.get(path, headers=headers).status_code == status
    assert (
        client.post("/ext/fill", json={"job_id": "j1", "fields": []}, headers=headers).status_code == status
    )
    assert client.post("/ext/applied", json={"job_id": "j1"}, headers=headers).status_code == status


def test_a_website_posing_as_localhost_is_refused(client: TestClient) -> None:
    rebinding = TestClient(client.app, base_url="http://attacker.example")
    assert rebinding.get("/ext/jobs", headers=paired()).status_code == 400


def test_pairing_needs_a_code_from_the_dashboard_and_wrong_codes_run_out(
    client: TestClient, actions: ExtensionActions
) -> None:
    assert (
        client.post(
            "/ext/pair", json={"code": "ABCD2345"}, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.post("/ext/pair", json={"code": "ABCD2345"}, headers=EXT).status_code == 403
    )  # no code shown yet
    page = client.get("/settings/extension").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page)
    assert csrf is not None and "Show a pairing code" in page
    assert client.get("/settings/extension").status_code == 200 and actions.codes == []  # a visit makes none
    shown = client.post("/settings/extension", data={"csrf": csrf.group(1)}, headers=HERE).text
    assert "ABCD2345" in shown
    assert client.post("/settings/extension", data={"csrf": "forged"}, headers=HERE).status_code == 403
    answer = client.post("/ext/pair", json={"code": "abcd2345"}, headers=EXT)
    assert answer.status_code == 200 and answer.json() == {"key": KEY}
    for _ in range(PAIR_TRIES - 1):  # one wrong try was made above, before a code was shown
        assert client.post("/ext/pair", json={"code": "NOPE0000"}, headers=EXT).status_code == 403
    assert client.post("/ext/pair", json={"code": "ABCD2345"}, headers=EXT).status_code == 429  # locked out


def test_fill_files_and_applied(client: TestClient, actions: ExtensionActions) -> None:
    jobs = client.get("/ext/jobs?url=https://acme.example/jobs/1", headers=paired()).json()
    assert jobs["matched"] == "j1" and jobs["jobs"][0]["note"] == "Apply yourself"
    fields = [
        {"key": "s-1", "label": "First name", "type": "text", "required": True},
        {"key": "s-2", "label": "Resume", "type": "file"},
        {"key": "s-3", "label": "X" * 5000, "type": "weird", "options": ["a"] * 500},
        {"label": "no key"},
        "junk",
    ]
    answer = client.post("/ext/fill", json={"job_id": "j1", "fields": fields}, headers=paired()).json()
    assert answer["answers"]["s-1"] == {"value": "Asha", "source": "profile"}
    assert answer["answers"]["s-2"] == {"value": "resume", "source": "file"}  # a kind, never a path
    assert answer["missing"] == ["s-3"]
    _, sent = actions.filled[-1]
    assert [f.key for f in sent] == ["s-1", "s-2", "s-3"]  # junk dropped
    odd = sent[2]
    assert odd.type == "unknown" and len(odd.label) == 500 and len(odd.options) == 100  # cut to size
    assert (
        client.post("/ext/fill", json={"job_id": "gone", "fields": []}, headers=paired()).status_code == 404
    )
    assert client.post("/ext/fill", content=b"not json", headers=paired()).status_code == 422
    pdf = client.get("/ext/file/j1/resume", headers=paired())
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert "Asha_Rao_Acme_Resume.pdf" in pdf.headers["content-disposition"]
    assert client.get("/ext/file/j1/cover", headers=paired()).status_code == 404
    assert client.get("/ext/file/j1/..%2F..%2Fetc%2Fpasswd", headers=paired()).status_code == 404
    done = client.post(
        "/ext/applied", json={"job_id": "j1", "answers": [{"label": "Notice period", "value": "30 days"}, 7]},
        headers=paired(),
    )  # fmt: skip
    assert done.status_code == 200
    assert actions.applied_with[-1] == ("j1", [("Notice period", "30 days")])


def test_the_extension_downloads_from_the_dashboard(client: TestClient) -> None:
    got = client.get("/extension.zip")
    assert got.status_code == 200 and got.content.startswith(b"PK")
    assert "sarthi-extension.zip" in got.headers["content-disposition"]


def test_without_a_running_sarthi_the_extension_api_says_so(
    conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock
) -> None:
    app = create_app(read_only(tmp_path / "data" / "sarthi.db"), clock=clock)
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/ext/jobs", headers=paired()).status_code == 503
