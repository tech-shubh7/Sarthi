"""The editable dashboard: settings, job actions, Submit/Cancel, and the guards every change must pass."""

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchRecord, MatchStore
from sarthi.profile.models import Preferences, Profile, RemotePreference
from sarthi.web.actions import Controls, MailSettings, MatchBatch, Pending
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
        self.applied: list[str] = []
        self.refilled: list[str] = []
        self.finished: list[str] = []

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

    def queue_matches(self) -> MatchBatch:
        return MatchBatch(scheduled=2)

    def pending(self) -> dict[str, Pending]:
        return self.waiting

    def submit(self, job_id: str) -> bool:
        self.sent.append(job_id)
        return self.waiting.pop(job_id, None) is not None

    def finish(self, job_id: str) -> bool:
        self.finished.append(job_id)
        return job_id != "busy"

    def refill(self, job_id: str) -> bool:
        self.refilled.append(job_id)
        return True

    def mark_applied(self, job_id: str) -> bool:
        self.applied.append(job_id)
        return job_id != "gone"

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return self.waiting.pop(job_id, None) is not None

    # without Telegram: see tests/web/test_activity.py
    answered: list[tuple[str, dict[str, str]]] = []  # noqa: RUF012  (replaced per test)
    declined: list[str] = []  # noqa: RUF012
    auto: list[tuple[str, str]] = []  # noqa: RUF012
    logins: list[str] = []  # noqa: RUF012
    screen: str | None = None

    def answer_questions(self, job_id: str, answers: dict[str, str]) -> dict[str, str]:
        self.answered = [*self.answered, (job_id, answers)]
        return {"visa": "Please pick one of the listed answers."} if answers.get("visa") == "Maybe" else {}

    def decline_questions(self, job_id: str) -> bool:
        self.declined = [*self.declined, job_id]
        return job_id != "gone"

    def cancel_auto(self, job_id: str) -> bool:
        self.auto = [*self.auto, ("cancel", job_id)]
        return True

    def apply_now(self, job_id: str) -> bool:
        self.auto = [*self.auto, ("now", job_id)]
        return job_id != "gone"

    def login_sites(self) -> list[tuple[str, str]]:
        return [("linkedin", "LinkedIn"), ("naukri", "Naukri")]

    def site_login(self, site: str) -> bool:
        self.logins = [*self.logins, site]
        return site != "naukri" or not self.screen

    def screen_link(self) -> str | None:
        return self.screen

    ai = True

    def ai_ready(self) -> bool:
        return self.ai

    # mail (reply tracking): see tests/web/test_mail_settings.py
    mail_settings = MailSettings("", 993, "", "INBOX", enabled=False)
    mail_saved: list[tuple[MailSettings, str]] = []  # noqa: RUF012  (replaced per test)
    mail_tested: list[tuple[MailSettings, str]] = []  # noqa: RUF012

    def mail(self) -> MailSettings:
        return self.mail_settings

    def save_mail(self, settings: MailSettings, password: str) -> None:
        self.mail_saved = [*self.mail_saved, (settings, password)]
        self.mail_settings = MailSettings(
            settings.host, settings.port, settings.user, settings.folder, settings.enabled,
            password_saved=settings.password_saved or bool(password),
        )  # fmt: skip

    async def test_mail(self, settings: MailSettings, password: str) -> str:
        self.mail_tested = [*self.mail_tested, (settings, password)]
        return (
            "Could not connect: the mailbox refused the login."
            if password.startswith("wrong")
            else "Connected: 3 messages in INBOX."
        )


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


def test_a_berlin_user_with_languages() -> None:
    berlin = FORM | {
        "home": "Germany",
        "countries": "none",
        "languages": "German B2, French",
        "salary": "€65.000",
    }
    result = profile_from_form(berlin, PROFILE)
    assert result.profile is not None, result.errors
    p = result.profile.preferences
    assert (p.home_country, p.languages) == ("DE", ("German B2", "French"))
    assert p.min_salary is not None and (p.min_salary.currency, p.min_salary.minimum) == ("EUR", 65_000)
    assert form_values(result.profile)["languages"] == "German B2, French"


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
    assert (response.status_code, response.headers["location"]) == (303, "/settings/profile?done=profile")
    assert actions.saved and actions.saved[-1].preferences.roles == ("AI Engineer", "Backend Engineer")
    assert "matched again" in client.get("/settings/profile?done=profile").text


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
    page = client.get("/settings/automation").text
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
    assert response.headers["location"] == f"/matches?done=apply#job-{job_id}"
    assert actions.chosen == [(job_id, "apply")]
    # Only known actions and only back to the dashboard's own pages.
    assert client.post(f"/jobs/{job_id}/delete", data={"csrf": good}, headers=HERE).status_code == 404
    elsewhere = client.post(
        f"/jobs/{job_id}/skip",
        data={"csrf": good, "next": "https://evil.example"},
        headers=HERE,
        follow_redirects=False,
    )
    assert elsewhere.headers["location"] == f"/matches?done=skip#job-{job_id}"
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
    page = client.get("/applications/needs-you").text
    assert "Waiting for your Submit" in page and f"/applications/{job_id}/submit" in page
    assert client.get(f"/applications/{job_id}/form.png").content == b"\x89PNG fake"
    good = token(client)
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


def test_after_apply_you_land_back_on_the_same_tab_page_and_job(
    client: TestClient, actions: FakeActions, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    # Seen live: Apply on page 2 of "Strong" sent the user back to page 1 of "All".
    job_id = job_store.save(make_job(title="AI Engineer", company="Acme")).job_id
    MatchStore(conn, clock=clock).save(MatchRecord(job_id, "1", 88, "alert", "strong", None, (), (), True))
    page = client.get("/matches?show=alert&q=AI&page=1").text
    assert 'value="/matches?show=alert&amp;q=AI&amp;page=1"' in page  # the form carries where it is
    assert f'id="job-{job_id}"' in page
    good = token(client, "/matches")
    response = client.post(
        f"/jobs/{job_id}/apply",
        data={"csrf": good, "next": "/matches?show=alert&q=AI&page=1&evil=1"},
        headers=HERE,
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/matches?show=alert&q=AI&page=1&done=apply#job-{job_id}"
    for bad in ("https://evil.example/matches", "//evil.example/x", "/elsewhere"):
        r = client.post(
            f"/jobs/{job_id}/skip", data={"csrf": good, "next": bad}, headers=HERE, follow_redirects=False
        )
        assert r.headers["location"].startswith("/matches?done=skip")


def test_apply_yourself_says_why_and_takes_i_applied(
    client: TestClient, actions: FakeActions, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    from sarthi.db.applications import ApplicationStatus, ApplicationStore  # noqa: PLC0415

    job_id = job_store.save(make_job(title="Backend Engineer", company="Beta")).job_id
    ApplicationStore(conn, clock=clock).record(
        job_id, ApplicationStatus.NEEDS_HUMAN, site="acme.wd5.myworkdayjobs.com",
        detail="Workday forms need your own Workday account for this employer.",
    )  # fmt: skip
    page = client.get("/applications/apply-yourself").text
    assert "Backend Engineer" in page and "Workday needs your own account" in page
    assert "Open &amp; apply" in page and f"/applications/{job_id}/applied" in page
    assert "Backend Engineer" not in client.get("/applications/needs-you").text
    good = token(client)
    response = client.post(
        f"/applications/{job_id}/applied", data={"csrf": good, "next": "/applications?page=1"}, headers=HERE,
        follow_redirects=False,
    )  # fmt: skip
    assert response.headers["location"] == "/applications?page=1&done=applied"
    assert actions.applied == [job_id]


def test_a_filled_form_lost_in_a_restart_offers_to_fill_it_again(
    client: TestClient, actions: FakeActions, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    from sarthi.db.applications import ApplicationStatus, ApplicationStore  # noqa: PLC0415

    job_id = job_store.save(make_job(title="Backend Engineer", company="Gamma")).job_id
    ApplicationStore(conn, clock=clock).record(job_id, ApplicationStatus.FILLED, site="jobs.lever.co")
    actions.waiting = {}  # its filled page is gone
    page = client.get("/applications/needs-you").text
    assert "previous browser tab is closed" in page and f"/applications/{job_id}/refill" in page
    good = token(client)
    response = client.post(
        f"/applications/{job_id}/refill", data={"csrf": good}, headers=HERE, follow_redirects=False
    )
    assert response.headers["location"].startswith("/applications?done=refill")
    assert actions.refilled == [job_id]


def test_a_captcha_form_offers_to_finish_it_on_this_computer(
    client: TestClient, actions: FakeActions, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    from sarthi.db.applications import ApplicationStatus, ApplicationStore  # noqa: PLC0415

    job_id = job_store.save(
        make_job(title="Data Engineer", company="Delta", url="https://jobs.example.org/delta/1")
    ).job_id
    other = job_store.save(
        make_job(title="QA Engineer", company="Echo", url="https://jobs.example.org/echo/2")
    ).job_id
    store = ApplicationStore(conn, clock=clock)
    store.record(
        job_id,
        ApplicationStatus.NEEDS_HUMAN,
        site="x.example",
        detail="The form has a CAPTCHA, which only you can solve.",
    )
    store.record(
        other, ApplicationStatus.NEEDS_HUMAN, site="y.example", detail="Workday forms need your own account."
    )
    page = client.get("/applications/captcha").text
    assert "Finish it myself" in page and "Sarthi never solves CAPTCHAs" in page
    assert f"/applications/{job_id}/finish" in page and f"/applications/{other}/finish" not in page
    good = token(client)
    response = client.post(
        f"/applications/{job_id}/finish", data={"csrf": good, "next": "/applications?page=1"}, headers=HERE,
        follow_redirects=False,
    )  # fmt: skip
    assert response.headers["location"] == "/applications?page=1&done=finish"
    assert actions.finished == [job_id]
    page = client.get(response.headers["location"]).text
    assert "Solve the CAPTCHA" in page


def test_finishing_by_hand_needs_a_free_browser(client: TestClient, actions: FakeActions) -> None:
    good = token(client)
    response = client.post(
        "/applications/busy/finish", data={"csrf": good}, headers=HERE, follow_redirects=False
    )
    assert response.headers["location"].startswith("/applications?done=busy")


def test_finishing_by_hand_checks_the_csrf_token(client: TestClient, actions: FakeActions) -> None:
    response = client.post(
        "/applications/job-1/finish", data={"csrf": "wrong"}, headers=HERE, follow_redirects=False
    )
    assert response.status_code == 403
    assert actions.finished == []


def test_bulk_apply_routes_to_the_in_progress_view(client: TestClient) -> None:
    page = client.get("/matches")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert csrf
    denied = client.post("/matches/queue", data={"csrf": csrf[1]}, follow_redirects=False)
    assert denied.status_code == 403
    response = client.post(
        "/matches/queue",
        data={"csrf": csrf[1]},
        headers={"origin": "http://127.0.0.1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/applications/in-progress?done=matching_queued"


def test_invalid_automation_settings_do_not_pause_alerts(client: TestClient, actions: FakeActions) -> None:
    csrf = token(client, "/settings/automation")
    response = client.post(
        "/settings/alerts", data={"csrf": csrf, "auto_line": "20", "paused": "on"}, headers=HERE
    )
    assert response.status_code == 400
    assert not actions.paused


def test_settings_sections_only_include_their_own_form(client: TestClient) -> None:
    profile = client.get("/settings/profile").text
    automation = client.get("/settings/automation").text
    assert 'action="/settings/profile"' in profile and 'action="/settings/alerts"' not in profile
    assert 'action="/settings/alerts"' in automation and 'action="/settings/profile"' not in automation
