import socket
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sarthi.db.answers import AnswerStore
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchRecord, MatchStore
from sarthi.db.replies import ReplyRecord, ReplyStore
from sarthi.db.source_state import SourceStateStore
from sarthi.jobs.forms import FormField
from sarthi.web import server
from sarthi.web.app import ago, create_app, read_only
from tests.conftest import FakeClock
from tests.matching.conftest import make_job

PAGES = ["/", "/matches", "/matches?show=alert", "/matches?show=digest", "/applications", "/sources"]


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock) -> TestClient:
    # The dashboard's own read-only connection on the database ``conn`` writes to, as when it runs.
    app = create_app(read_only(tmp_path / "data" / "sarthi.db"), clock=clock)
    return TestClient(app, base_url="http://127.0.0.1")


def match(conn: sqlite3.Connection, clock: FakeClock, job_id: str, score: int, decision: str) -> None:
    MatchStore(conn, clock=clock).save(
        MatchRecord(job_id, "1", score, decision, "strong", None, ("Relevant role",), (), False)
    )


@pytest.mark.parametrize("path", PAGES)
def test_every_page_works_on_an_empty_database(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert "<!doctype html>" in response.text


def test_pages_show_matches_applications_replies_and_sources(
    client: TestClient, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    strong = job_store.save(make_job(title="AI Backend Engineer", company="Acme")).job_id
    fair = job_store.save(make_job(url="https://jobs.example/2", title="Data Analyst", company="Beta")).job_id
    hidden = job_store.save(
        make_job(url="https://jobs.example/3", title="Senior Architect", company="Gamma")
    ).job_id
    match(conn, clock, strong, 82, "alert")
    match(conn, clock, fair, 48, "digest")
    match(conn, clock, hidden, 0, "hide")
    ChatStore(conn, clock=clock).set_job_status(strong, JobStatus.APPLY)
    ApplicationStore(conn, clock=clock).record(strong, ApplicationStatus.SUBMITTED, site="greenhouse")
    ReplyStore(conn, clock=clock).save(ReplyRecord("m1", strong, "interview", received_at=clock()))
    SourceStateStore(conn, clock=clock).record_failure("feed:remotive", "RetryableError: HTTP 503")

    overview = client.get("/").text
    assert ">3<" in overview  # jobs found
    matches = client.get("/matches").text
    assert "AI Backend Engineer" in matches
    assert "Why it matched" in matches and "Relevant role" in matches
    assert "Data Analyst" in matches
    assert "Senior Architect" not in matches  # hidden jobs are not matches
    assert matches.index("AI Backend Engineer") < matches.index("Data Analyst")  # best first
    assert "Applying" in matches
    assert "Data Analyst" not in client.get("/matches?show=alert").text
    applications = client.get("/applications").text
    assert "Submitted" in applications
    assert "Interview" in applications
    sources = client.get("/sources").text
    assert "remotive" in sources and "feed" in sources
    assert "HTTP 503" in sources


def test_old_matches_are_left_out(
    client: TestClient, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    old = job_store.save(make_job(title="Old Role")).job_id
    match(conn, clock, old, 90, "alert")
    clock.advance(timedelta(days=8).total_seconds())
    assert "Old Role" not in client.get("/matches").text


def test_job_text_is_never_run_as_markup(
    client: TestClient, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    evil = job_store.save(
        make_job(title="<script>alert(1)</script> Dev", company="<img src=x onerror=1>")
    ).job_id
    match(conn, clock, evil, 80, "alert")
    page = client.get("/matches").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "<img src=x" not in page


def test_security_headers_and_this_computer_only(client: TestClient) -> None:
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    assert "script-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    # A site pointing its own name at 127.0.0.1 (DNS rebinding) is refused.
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"host": "localhost:8765"}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/matches", "/applications"])
def test_nothing_can_be_changed(client: TestClient, path: str) -> None:
    assert client.post(path).status_code == 405


def test_odd_filters_and_unknown_pages(client: TestClient) -> None:
    assert client.get("/matches?show=<script>").status_code == 200
    assert client.get("/jobs/../../etc/passwd").status_code == 404
    assert client.get("/docs").status_code == 404  # no API browser


def test_static_files_are_versioned(client: TestClient) -> None:
    page = client.get("/").text
    href = page.split('href="/static/app.css?v=', 1)[1].split('"', 1)[0]
    assert len(href) == 10
    css = client.get(f"/static/app.css?v={href}")
    assert css.status_code == 200
    assert css.headers["cache-control"] == "no-cache"


def test_the_dashboard_connection_cannot_write(conn: sqlite3.Connection, tmp_path: Path) -> None:
    ro = read_only(tmp_path / "data" / "sarthi.db")
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM job")
    ro.close()


@pytest.mark.parametrize(
    ("iso", "text"),
    [
        (None, ""),
        ("", ""),
        ("not a time", ""),
        ("2026-11-20T08:59:30+00:00", "just now"),
        ("2026-11-20T08:48:00+00:00", "12 min ago"),
        ("2026-11-20T06:00:00", "3 h ago"),  # no zone: read as UTC
        ("2026-11-17T09:00:00+00:00", "3 days ago"),
        ("2026-11-21T09:00:00+00:00", "just now"),  # in the future: never negative
    ],
)
def test_ago(iso: str | None, text: str, clock: FakeClock) -> None:
    assert ago(iso, clock()) == text


def test_a_busy_port_moves_to_the_next_free_one() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert server.free_port(port) != port


def test_only_this_computer_or_a_container_can_be_the_host(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dashboard host"):
        server.make_server(tmp_path / "x.db", 8765, host="192.168.1.10")


def test_no_free_port_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_bindable", lambda port, host="": False)
    with pytest.raises(server.NoFreePortError, match="SARTHI_DASHBOARD_PORT"):
        server.free_port(8765)


def test_the_link_to_connect_telegram_is_shown_until_used(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    assert "Connect Telegram" not in client.get("/").text
    chats = ChatStore(conn)
    chats.set_state("telegram.pair_link", "https://t.me/SarthiJobsBot?start=AB12CD")
    page = client.get("/").text
    assert "Connect Telegram" in page
    assert 'href="https://t.me/SarthiJobsBot?start=AB12CD"' in page
    chats.set_state("telegram.pair_link", "javascript:alert(1)")  # never anything but a Telegram link
    assert "Connect Telegram" not in client.get("/").text
    chats.set_state("telegram.pair_link", None)
    assert "Connect Telegram" not in client.get("/").text


def test_application_filters_show_distinct_states_and_include_unanswered_questions(
    client: TestClient, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    ask = job_store.save(make_job(url="https://jobs.example/1", title="Question Engineer")).job_id
    manual = job_store.save(make_job(url="https://jobs.example/2", title="Manual Designer")).job_id
    sent = job_store.save(make_job(url="https://jobs.example/3", title="Sent Analyst")).job_id
    captcha = job_store.save(make_job(url="https://jobs.example/5", title="Captcha Specialist")).job_id
    uncertain = job_store.save(make_job(url="https://jobs.example/4", title="Uncertain Recruiter")).job_id
    AnswerStore(conn, clock=clock).set_pending(ask, [FormField("why", "Why us?", "textarea", True)])
    store = ApplicationStore(conn, clock=clock)
    store.record(manual, ApplicationStatus.NEEDS_HUMAN, detail="Application site requires a login")
    store.record(sent, ApplicationStatus.SUBMITTED)
    store.record(captcha, ApplicationStatus.NEEDS_HUMAN, detail="CAPTCHA required")
    store.record(
        uncertain, ApplicationStatus.NEEDS_HUMAN, detail="Submit was pressed: it may have been sent."
    )

    roles = {
        "input": "Question Engineer",
        "manual": "Manual Designer",
        "captcha": "Captcha Specialist",
        "submitted": "Sent Analyst",
        "verify": "Uncertain Recruiter",
    }
    for tab, title in roles.items():
        result = client.get(f"/applications/{tab}")
        assert result.status_code == 200
        assert title in result.text
        assert all(other not in result.text for other in roles.values() if other != title)
    assert f"/activity/questions?job={ask}#job-{ask}" in client.get("/applications?show=input").text
    assert "Open &amp; apply" not in client.get("/applications?show=verify").text


def test_application_search_site_filter_and_sort(
    client: TestClient, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    store = ApplicationStore(conn, clock=clock)
    for index, (company, site) in enumerate(
        (("Zeta Studio", "ats.example"), ("Alpha Works", "ats.example"), ("Other Group", "other.example"))
    ):
        job_id = job_store.save(
            make_job(
                url=f"https://jobs.example/{index}",
                title="Backend Specialist",
                company=company,
            )
        ).job_id
        store.record(job_id, ApplicationStatus.NEEDS_HUMAN, site=site, detail="Apply at employer site")
        clock.advance(2)
    html = client.get(
        "/applications/manual", params={"q": "backend", "site": "ats.example", "sort": "company"}
    ).text
    assert html.index("Alpha Works") < html.index("Zeta Studio")
    assert "Other Group" not in html
    oldest = client.get("/applications/manual", params={"sort": "oldest"}).text
    assert oldest.index("Zeta Studio") < oldest.index("Alpha Works")
    assert "Alpha Works" not in client.get("/applications/manual", params={"q": "%"}).text


@pytest.mark.parametrize(
    "path", ["/applications/missing", "/settings/missing", "/activity/missing", "/sources/missing"]
)
def test_unknown_section_fails_with_404(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404
