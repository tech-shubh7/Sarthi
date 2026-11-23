"""The editable dashboard: settings, job actions, Submit/Cancel, and the guards every change must pass."""

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchRecord, MatchStore
from sarthi.profile.models import Preferences, Profile, RemotePreference
from sarthi.web.actions import Controls, Pending
from sarthi.web.app import create_app, read_only
from sarthi.web.settings_form import form_values, profile_from_form
from tests.conftest import FakeClock
from tests.matching.conftest import make_job

HERE = {"origin": "http://127.0.0.1"}
PROFILE = Profile(
    preferences=Preferences(roles=("AI Engineer",), home_country="IN", years_experience=1),
    skills=("Python", "LangGraph"),
    resume_text="Aarav Mehta. Python, LangGraph.",
)


class FakeActions:
    def __init__(self, profile: Profile | None = PROFILE) -> None:
        self.saved: list[Profile] = []
        self.current = profile
        self.paused = False
        self.line: int | None = None
        self.chosen: list[tuple[str, str]] = []
        self.waiting = {"job-1": Pending("job-1", "screenshots/job-1.png")}
        self.sent: list[str] = []
        self.cancelled: list[str] = []

    def profile(self) -> Profile | None:
        return self.current

    def save_profile(self, profile: Profile) -> int:
        self.saved.append(profile)
        self.current = profile
        return 3

    def resume_text(self, data: bytes) -> str:
        if not data.strip():
            raise ValueError("I found almost no text.")
        return data.decode()

    def controls(self) -> Controls:
        return Controls(self.paused, self.line, 120, 10)

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def set_auto_line(self, line: str) -> str | None:
        if line == "off":
            self.line = None
            return None
        if not line.isdigit() or not 75 <= int(line) <= 100:
            return "Pick a line from 75 to 100."
        self.line = int(line)
        return None

    def choose(self, job_id: str, action: str) -> bool:
        self.chosen.append((job_id, action))
        return job_id != "gone"

    def pending(self) -> dict[str, Pending]:
        return self.waiting

    def submit(self, job_id: str) -> bool:
        self.sent.append(job_id)
        return self.waiting.pop(job_id, None) is not None

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return self.waiting.pop(job_id, None) is not None


@pytest.fixture
def actions() -> FakeActions:
    return FakeActions()


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock, actions: FakeActions) -> TestClient:
    app = create_app(
        read_only(tmp_path / "data" / "sarthi.db"), clock=clock, actions=actions, data_dir=tmp_path
    )
    return TestClient(app, base_url="http://127.0.0.1")


def token(client: TestClient, path: str = "/settings") -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert found is not None
    return found.group(1)


FORM = {
    "roles": "AI Engineer, Backend Engineer",
    "skills": "Python, LangGraph, RAG",
    "home": "India",
    "countries": "UAE",
    "visa_free": "none",
    "remote": "ok",
    "years": "1.5",
    "salary": "6 LPA",
    "exclude_companies": "Acme Spam",
    "looking_for": "LLM products",
    "ruled_out": "sales",
}


# ---- the form, read the way the chat reads answers ----


def test_the_form_reads_like_the_telegram_setup() -> None:
    result = profile_from_form(FORM, PROFILE)
    assert result.profile is not None, result.errors
    p = result.profile.preferences
    assert p.roles == ("AI Engineer", "Backend Engineer")
    assert (p.home_country, p.countries, p.authorized_countries) == ("IN", frozenset({"AE"}), frozenset())
    assert (p.years_experience, p.remote) == (1.5, RemotePreference.OK)
    assert p.min_salary is not None and p.min_salary.minimum == 600_000
    assert result.profile.skills == ("Python", "LangGraph", "RAG")
    assert result.profile.resume_text == PROFILE.resume_text  # kept unless a new one is uploaded


@pytest.mark.parametrize(
    ("field", "value"),
    [("roles", ""), ("home", "Atlantis"), ("home", "India, UK"), ("years", "lots"), ("salary", "a lot")],
)
def test_bad_values_are_explained_and_nothing_is_saved(field: str, value: str) -> None:
    result = profile_from_form(FORM | {field: value}, PROFILE)
    assert result.profile is None
    assert field in result.errors
    assert result.values[field] == value  # what was typed comes back, not lost


def test_the_saved_profile_fills_the_form_back() -> None:
    saved = profile_from_form(FORM, PROFILE).profile
    assert saved is not None
    values = form_values(saved)
    again = profile_from_form(values, saved).profile
    assert again == saved


# ---- the guards ----


def test_a_change_from_another_site_or_without_the_token_is_refused(
    client: TestClient, actions: FakeActions
) -> None:
    good = token(client)
    assert (
        client.post("/settings/alerts", data={"csrf": good, "paused": "on"}).status_code == 403
    )  # no Origin
    evil = {"origin": "https://evil.example"}
    assert (
        client.post("/settings/alerts", data={"csrf": good, "paused": "on"}, headers=evil).status_code == 403
    )
    assert (
        client.post("/settings/alerts", data={"csrf": "guess", "paused": "on"}, headers=HERE).status_code
        == 403
    )
    assert actions.paused is False  # nothing changed
    response = client.post(
        "/settings/alerts", data={"csrf": good, "paused": "on"}, headers=HERE, follow_redirects=False
    )
    assert response.status_code == 303
    assert actions.paused is True


def test_a_page_posing_as_localhost_is_refused(client: TestClient) -> None:
    rebinding = TestClient(client.app, base_url="http://attacker.example")
    assert rebinding.get("/settings").status_code == 400


def test_without_a_running_sarthi_the_dashboard_only_reads(
    conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock
) -> None:
    app = create_app(read_only(tmp_path / "data" / "sarthi.db"), clock=clock)
    client = TestClient(app, base_url="http://127.0.0.1")
    page = client.get("/settings").text
    assert "Read-only" in page and "Save profile" not in page
    found = re.search(r'name="csrf" value="([^"]+)"', page)
    data = {"csrf": found.group(1) if found else ""} | FORM
    assert client.post("/settings/profile", data=data, headers=HERE).status_code == 403


def test_pages_set_strict_headers(client: TestClient) -> None:
    headers = client.get("/settings").headers
    assert "form-action 'self'" in headers["content-security-policy"]
    assert "script-src 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"


# ---- settings ----


def test_saving_the_profile(client: TestClient, actions: FakeActions) -> None:
    response = client.post(
        "/settings/profile", data={"csrf": token(client)} | FORM, headers=HERE, follow_redirects=False
    )
    assert (response.status_code, response.headers["location"]) == (303, "/settings?done=profile")
    assert actions.saved and actions.saved[-1].preferences.roles == ("AI Engineer", "Backend Engineer")
    assert "matched again" in client.get("/settings?done=profile").text


def test_a_bad_field_comes_back_with_its_message(client: TestClient, actions: FakeActions) -> None:
    response = client.post(
        "/settings/profile", data={"csrf": token(client)} | FORM | {"home": "Atlantis"}, headers=HERE
    )
    assert response.status_code == 400
    assert "Write the one country you live in" in response.text
    assert 'value="Atlantis"' in response.text
    assert not actions.saved


def test_a_first_profile_needs_a_resume_and_takes_one(client: TestClient, actions: FakeActions) -> None:
    actions.current = None
    data = {"csrf": token(client)} | FORM
    assert "Add your resume" in client.post("/settings/profile", data=data, headers=HERE).text
    empty = {"resume": ("cv.txt", b"   ", "text/plain")}
    assert "almost no text" in client.post("/settings/profile", data=data, files=empty, headers=HERE).text
    resume = {"resume": ("cv.txt", b"Aarav Mehta. Python, FastAPI.", "text/plain")}
    response = client.post("/settings/profile", data=data, files=resume, headers=HERE, follow_redirects=False)
    assert response.status_code == 303
    assert actions.saved[-1].resume_text == "Aarav Mehta. Python, FastAPI."


def test_auto_apply_and_pause_switches(client: TestClient, actions: FakeActions) -> None:
    good = token(client)
    client.post("/settings/alerts", data={"csrf": good, "auto_line": "85"}, headers=HERE)
    assert (actions.line, actions.paused) == (85, False)
    page = client.get("/settings").text
    assert '<option value="85" selected>' in page
    refused = client.post("/settings/alerts", data={"csrf": good, "auto_line": "40"}, headers=HERE)
    assert refused.status_code == 400 and "75 to 100" in refused.text
    client.post("/settings/alerts", data={"csrf": good, "auto_line": "off"}, headers=HERE)
    assert actions.line is None


# ---- jobs and applications ----


def test_apply_skip_and_later_from_the_matches_page(
    client: TestClient, actions: FakeActions, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    job_id = job_store.save(make_job(title="AI Engineer", company="Acme")).job_id
    MatchStore(conn, clock=clock).save(MatchRecord(job_id, "1", 88, "alert", "strong", None, (), (), True))
    page = client.get("/matches").text
    assert f'action="/jobs/{job_id}/apply"' in page
    good = token(client, "/matches")
    response = client.post(
        f"/jobs/{job_id}/apply", data={"csrf": good, "next": "/matches"}, headers=HERE, follow_redirects=False
    )
    assert response.headers["location"] == "/matches?done=apply"
    assert actions.chosen == [(job_id, "apply")]
    # Only known actions and only back to the dashboard's own pages.
    assert client.post(f"/jobs/{job_id}/delete", data={"csrf": good}, headers=HERE).status_code == 404
    elsewhere = client.post(
        f"/jobs/{job_id}/skip",
        data={"csrf": good, "next": "https://evil.example"},
        headers=HERE,
        follow_redirects=False,
    )
    assert elsewhere.headers["location"] == "/matches?done=skip"
    assert client.post("/jobs/gone/later", data={"csrf": good}, headers=HERE).status_code == 404


def test_a_filled_form_can_be_submitted_or_cancelled_here(
    client: TestClient,
    actions: FakeActions,
    conn: sqlite3.Connection,
    job_store: JobStore,
    clock: FakeClock,
    tmp_path: Path,
) -> None:
    from sarthi.db.applications import ApplicationStatus, ApplicationStore  # noqa: PLC0415

    job_id = job_store.save(make_job(title="AI Engineer", company="Acme")).job_id
    actions.waiting = {job_id: Pending(job_id, f"screenshots/{job_id}.png")}
    ApplicationStore(conn, clock=clock).record(job_id, ApplicationStatus.FILLED, site="jobs.lever.co")
    (tmp_path / "screenshots").mkdir()
    (tmp_path / "screenshots" / f"{job_id}.png").write_bytes(b"\x89PNG fake")
    page = client.get("/applications").text
    assert "Waiting for your Submit" in page and f"/applications/{job_id}/submit" in page
    assert client.get(f"/applications/{job_id}/form.png").content == b"\x89PNG fake"
    good = token(client, "/applications")
    response = client.post(
        f"/applications/{job_id}/submit", data={"csrf": good}, headers=HERE, follow_redirects=False
    )
    assert response.headers["location"] == "/applications?done=submitted"
    assert actions.sent == [job_id]
    again = client.post(
        f"/applications/{job_id}/cancel", data={"csrf": good}, headers=HERE, follow_redirects=False
    )
    assert again.headers["location"] == "/applications?done=gone"  # already sent: nothing to cancel


def test_only_screenshots_inside_the_data_folder_are_served(
    client: TestClient, actions: FakeActions, tmp_path: Path
) -> None:
    outside = tmp_path.parent / "secret.png"
    outside.write_bytes(b"\x89PNG secret")
    actions.waiting = {"job-x": Pending("job-x", "../secret.png"), "job-y": Pending("job-y", "profile.json")}
    (tmp_path / "profile.json").write_text("{}")
    assert client.get("/applications/job-x/form.png").status_code == 404
    assert client.get("/applications/job-y/form.png").status_code == 404
    assert client.get("/applications/nope/form.png").status_code == 404
