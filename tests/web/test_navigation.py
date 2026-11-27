"""One page per purpose: every page has its own address, the sidebar shows where you are, old links still work."""

import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sarthi.clock import to_iso
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskStore
from sarthi.web.app import create_app, read_only
from sarthi.web.application_view import PAGES, login_for
from tests.conftest import FakeClock
from tests.matching.conftest import make_job
from tests.web.test_settings import HERE, FakeActions, token

NEW_PAGES = [
    "/applications", *(p.href for p in PAGES),
    "/settings/profile", "/settings/automation", "/settings/logins", "/settings/email", "/settings/telegram",
    "/activity/timeline", "/activity/questions", "/activity/auto-apply",
    "/sources/readers", "/sources/sites", "/sources/companies",
]  # fmt: skip
CAPTCHA = "The form has a CAPTCHA, which only you can solve."
SIGNED_OUT = "{site} shows you as signed out: nothing was sent. Log in once (Settings or /login)."


@pytest.fixture
def actions() -> FakeActions:
    return FakeActions()


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock, actions: FakeActions) -> TestClient:
    app = create_app(
        read_only(tmp_path / "data" / "sarthi.db"), clock=clock, actions=actions, data_dir=tmp_path
    )
    return TestClient(app, base_url="http://127.0.0.1")


def handed_over(
    conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock, n: int, site: str, detail: str
) -> str:
    job_id = job_store.save(
        make_job(url=f"https://{site}/job/{n}", title=f"Role {n:02d}", company=f"Company {n:02d}")
    ).job_id
    ApplicationStore(conn, clock=clock).record(
        job_id, ApplicationStatus.NEEDS_HUMAN, site=site, detail=detail
    )
    return job_id


@pytest.fixture
def blocked(conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock) -> dict[str, list[str]]:
    """2 CAPTCHAs, 3 jobs waiting for a LinkedIn login, 1 for Indeed (no login offered), 1 submitted."""
    jobs: dict[str, list[str]] = {"captcha": [], "linkedin": [], "indeed": []}
    for n in range(2):
        jobs["captcha"].append(handed_over(conn, job_store, clock, n, "x.example", CAPTCHA))
    for n in range(2, 5):
        site = "www.linkedin.com"
        jobs["linkedin"].append(handed_over(conn, job_store, clock, n, site, SIGNED_OUT.format(site=site)))
    jobs["indeed"].append(
        handed_over(
            conn, job_store, clock, 5, "in.indeed.com", "in.indeed.com needs your own logged-in account."
        )
    )
    sent = job_store.save(make_job(url="https://jobs.example/sent", title="Sent Role")).job_id
    ApplicationStore(conn, clock=clock).record(sent, ApplicationStatus.SUBMITTED)
    return jobs


@pytest.mark.parametrize("path", NEW_PAGES)
def test_every_page_has_its_own_address(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200
    assert "view-tabs" not in response.text  # no tabs inside a page: each one is a page of its own
    assert "<title>" in response.text and "· Sarthi</title>" in response.text


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/applications/input", "/applications/needs-you"),
        ("/applications/manual?q=data&sort=company", "/applications/apply-yourself?q=data&sort=company"),
        ("/applications/queued", "/applications/in-progress"),
        ("/applications/stopped", "/applications/closed"),
        ("/applications?show=captcha&page=2", "/applications/captcha?page=2"),
        ("/applications?show=nonsense", "/applications/all"),
        ("/settings", "/settings/profile"),
        ("/activity", "/activity/timeline"),
        ("/activity?job=j1", "/activity/questions?job=j1"),
        ("/sources", "/sources/readers"),
    ],
)
def test_old_addresses_lead_to_the_page_now(client: TestClient, old: str, new: str) -> None:
    response = client.get(old, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (302, new)


def test_the_summary_counts_each_state_and_links_to_its_page(
    client: TestClient, blocked: dict[str, list[str]]
) -> None:
    page = client.get("/applications").text
    cards = dict(
        re.findall(
            r'href="/applications/([a-z-]+)">\s*<span class="state-title">.*?state-n">(\d+)<', page, re.S
        )
    )
    assert cards == {
        "needs-you": "0", "captcha": "2", "login-needed": "4", "apply-yourself": "0", "verify": "0",
        "in-progress": "0", "submitted": "1", "closed": "0",
    }  # fmt: skip
    assert 'class="card state-card blocked" href="/applications/captcha"' in page
    assert "Role 00" not in page  # the summary only counts: the jobs are on each state's page


def test_the_sidebar_marks_the_page_and_badges_what_waits(
    client: TestClient, blocked: dict[str, list[str]]
) -> None:
    page = client.get("/applications/captcha").text
    assert (
        '<a href="/applications/captcha" aria-current="page"><span>Solve CAPTCHA</span><b class="count hot"'
        in page
    )
    assert re.search(r'<span>Login needed</span><b class="count hot" title="Waiting for you">4</b>', page)
    assert re.search(r'<span>Applications</span><b class="count hot" title="Waiting for you">6</b>', page)
    assert '<nav class="crumbs"' in page and '<a href="/applications">Applications</a>' in page
    assert "<title>Solve CAPTCHA · Applications · Sarthi</title>" in page
    settings = client.get("/settings/email").text
    assert '<a href="/settings/email" aria-current="page">' in settings
    assert '<a href="/settings/profile" aria-current="page">' not in settings
    assert '<a href="/applications/captcha">' in settings  # Applications' pages show from every page


def test_each_state_page_lists_only_its_own_jobs_with_its_own_search(
    client: TestClient, blocked: dict[str, list[str]]
) -> None:
    captcha = client.get("/applications/captcha").text
    assert 'action="/applications/captcha" role="search"' in captcha
    assert "Role 00" in captcha and "Role 01" in captcha and "Role 02" not in captcha
    for job_id in blocked["captcha"]:
        assert f'action="/applications/{job_id}/finish"' in captcha and "Finish it myself" in captcha
    assert "Role 01" not in client.get("/applications/captcha", params={"q": "Role 00"}).text
    everything = client.get("/applications/all").text
    assert all(f"Role {n:02d}" in everything for n in range(6)) and "Sent Role" in everything


def test_in_progress_separates_processing_from_waiting(
    client: TestClient, job_store: JobStore, task_store: TaskStore, clock: FakeClock
) -> None:
    processing = job_store.save(
        make_job(url="https://jobs.example/processing", title="Processing role")
    ).job_id
    retrying = job_store.save(make_job(url="https://jobs.example/retrying", title="Retrying role")).job_id
    queued = job_store.save(make_job(url="https://jobs.example/queued", title="Queued role")).job_id
    task_store.enqueue("documents", processing, {"apply": True})
    task_store.enqueue("documents", retrying, {"apply": True})
    task_store.enqueue("documents", queued, {"apply": True}, run_after=to_iso(clock() + timedelta(hours=1)))
    first = task_store.claim()
    second = task_store.claim()
    assert first and first.key == processing
    assert second and second.key == retrying
    task_store.fail(second.id, "temporary failure", to_iso(clock() + timedelta(minutes=5)))

    page = client.get("/applications/in-progress").text
    for job_id, label in [(processing, "Processing"), (retrying, "Waiting to retry"), (queued, "Queued")]:
        assert re.search(rf'id="job-{job_id}">(?:(?!</tr>).)*>{label}</span>', page, re.S)


def test_login_needed_groups_jobs_by_site_with_a_login_each(
    client: TestClient, actions: FakeActions, blocked: dict[str, list[str]]
) -> None:
    page = client.get("/applications/login-needed").text
    linkedin, indeed = page.index('<h2 id="site-1">www.linkedin.com</h2>'), page.index(">in.indeed.com</h2>")
    assert linkedin < indeed  # the site with the most jobs first
    assert '<b class="num">3</b> jobs are waiting' in page and '<b class="num">1</b> job is waiting' in page
    assert page.count('action="/settings/login/linkedin"') == 1 and "Log in to LinkedIn" in page
    assert "Sarthi has no login window for this site" in page  # Indeed: not a login this dashboard offers
    assert 'name="next" value="/applications/login-needed"' in page
    assert all(f"/applications/{job}/applied" in page for job in blocked["linkedin"] + blocked["indeed"])
    assert "Role 00" not in page  # a CAPTCHA, not a login

    good = token(client)
    sent = client.post(
        "/settings/login/linkedin", data={"csrf": good, "next": "/applications/login-needed"}, headers=HERE,
        follow_redirects=False,
    )  # fmt: skip
    assert sent.headers["location"] == "/applications/login-needed?done=login"
    elsewhere = client.post(
        "/settings/login/linkedin", data={"csrf": good, "next": "https://evil.example/"}, headers=HERE,
        follow_redirects=False,
    )  # fmt: skip
    assert elsewhere.headers["location"] == "/settings/logins?done=login"
    assert actions.logins == ["linkedin", "linkedin"]
    assert "4 jobs wait for a login" in client.get("/settings/logins").text


@pytest.mark.parametrize(
    ("path", "data", "headers"),
    [
        ("/settings/login/linkedin", {"csrf": "wrong", "next": "/applications/login-needed"}, HERE),
        ("/settings/login/linkedin", None, {"origin": "https://evil.example"}),
        ("/applications/j1/finish", None, {"origin": "https://evil.example"}),
        ("/applications/j1/applied", {"csrf": "wrong"}, HERE),
    ],
)
def test_actions_on_the_new_pages_need_the_token_and_this_computer(
    client: TestClient, actions: FakeActions, path: str, data: dict[str, str] | None, headers: dict[str, str]
) -> None:
    form = data if data is not None else {"csrf": token(client)}
    assert client.post(path, data=form, headers=headers).status_code == 403
    assert actions.logins == [] and actions.finished == [] and actions.applied == []


def test_the_overview_says_first_what_needs_you(client: TestClient, blocked: dict[str, list[str]]) -> None:
    page = client.get("/").text
    panel = page[page.index('aria-labelledby="needs-title"') : page.index("</section>")]
    assert re.search(
        r'href="/applications/captcha"><span>CAPTCHA to solve</span><b class="count hot">2<', panel
    )
    assert re.search(
        r'href="/applications/login-needed"><span>Job sites to log in to</span><b class="count hot">4<', panel
    )
    assert "Applications to send yourself" not in panel  # nothing waits there: not listed
    assert page.index("needs-title") < page.index('class="stats"')  # at the top


def test_without_blockers_the_overview_says_so(client: TestClient) -> None:
    assert "Nothing needs you" in client.get("/").text


def test_a_read_only_dashboard_offers_no_login(
    conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock, blocked: dict[str, list[str]]
) -> None:
    client = TestClient(
        create_app(read_only(tmp_path / "data" / "sarthi.db"), clock=clock), base_url="http://127.0.0.1"
    )
    page = client.get("/applications/login-needed").text
    assert "www.linkedin.com" in page and "Log in to" not in page and "I applied" not in page


@pytest.mark.parametrize(
    ("host", "login"),
    [
        ("in.indeed.com", ("indeed", "Indeed")),
        ("www.linkedin.com", ("linkedin", "LinkedIn")),
        ("notlinkedin.example", None),  # a name inside another word is not the site
        ("", None),
    ],
)
def test_a_site_is_matched_to_its_login_by_name(host: str, login: tuple[str, str] | None) -> None:
    assert login_for(host, [("linkedin", "LinkedIn"), ("indeed", "Indeed")]) == login
