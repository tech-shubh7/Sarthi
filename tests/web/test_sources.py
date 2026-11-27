import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sarthi.app import site_map
from sarthi.db.dashboard import DashboardQueries
from sarthi.db.jobs import JobStore
from sarthi.db.source_state import SourceStateStore
from sarthi.queue.tasks import Priority, TaskQueue
from sarthi.sources.sites import career_systems, reader_of
from sarthi.web.app import PAGE_SIZE, create_app, read_only
from tests.conftest import FakeClock
from tests.matching.conftest import make_job

READ = [
    "boards:linkedin+indeed",
    "search:freehire",
    "catalog:ats-scrapers",
    "feed:remotive",
    "feed:hackernews",
    "boards:adzuna",
]


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock) -> TestClient:
    app = create_app(read_only(tmp_path / "data" / "sarthi.db"), clock=clock, sites=site_map())
    return TestClient(app, base_url="http://127.0.0.1")


def save(jobs: JobStore, source: str, company: str, n: int) -> None:
    slug = f"{source}-{company}".lower().replace(" ", "-")
    for i in range(n):
        # A different role each: the same company and role is one job (a repost), whatever the site.
        url = f"https://jobs.example/{slug}/{i}"
        jobs.save(make_job(source=source, company=company, url=url, title=f"Platform Engineer {i}"))


def test_career_systems_are_hiring_systems_not_boards_or_feeds() -> None:
    systems = set(career_systems())
    assert {"greenhouse", "lever", "ashby", "keka", "darwinbox"} <= systems
    assert not {"linkedin", "naukri", "remoteok", "weworkremotely", "freehire", "hackernews"} & systems


@pytest.mark.parametrize(
    ("site", "reader"),
    [
        ("linkedin", "boards:linkedin+indeed"),
        ("naukri", "boards:linkedin+indeed"),  # India's own board, searched with the others
        ("adzuna", "boards:adzuna"),
        ("freehire", "search:freehire"),
        ("remotive", "feed:remotive"),
        ("hackernews", "feed:hackernews"),
        ("greenhouse", "catalog:ats-scrapers"),
        ("arbeitnow", None),  # no longer read
        ("made-up-site", None),
    ],
)
def test_each_site_is_credited_to_the_source_that_reads_it(site: str, reader: str | None) -> None:
    assert reader_of(site, READ) == reader


def test_a_watched_company_board_reads_its_system_when_the_snapshot_is_off() -> None:
    assert reader_of("lever", ["lever:example-labs", "feed:remotive"]) == "lever:example-labs"
    assert reader_of("lever", ["feed:remotive"]) is None


def test_queries_group_sites_and_career_pages(
    conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock
) -> None:
    save(job_store, "greenhouse", "Acme Labs", 3)
    job_store.save(  # the same company, spelled differently
        make_job(
            source="greenhouse", company="acme labs", url="https://jobs.example/x", title="Data Engineer"
        )
    )
    save(job_store, "lever", "Globex Works", 2)
    save(job_store, "linkedin", "Initech", 4)
    queries = DashboardQueries(read_only_conn(conn))
    sites = {s.site: s for s in queries.sites(clock() - timedelta(days=7))}
    assert (sites["greenhouse"].jobs, sites["greenhouse"].companies, sites["greenhouse"].jobs_since) == (
        4,
        1,
        4,
    )
    assert sites["linkedin"].jobs == 4
    assert queries.sites(clock() + timedelta(days=1))[0].jobs_since == 0
    pages = queries.career_pages(["greenhouse", "lever"])
    assert [(p.company.lower(), p.system, p.jobs) for p in pages] == [
        ("acme labs", "greenhouse", 4),
        ("globex works", "lever", 2),
    ]
    assert queries.count_career_pages(["greenhouse", "lever"]) == 2
    assert queries.career_pages([]) == [] and queries.count_career_pages([]) == 0
    assert [p.company for p in queries.career_pages(["greenhouse", "lever"], 1, offset=1)] == ["Globex Works"]


def read_only_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    return conn


def test_queued_sources_show_even_before_their_first_read(conn: sqlite3.Connection, queue: TaskQueue) -> None:
    SourceStateStore(conn).record_success("feed:remotive")
    queue.enqueue("source_check", "feed:remotive", priority=Priority.SOURCE)
    queue.enqueue("source_check", "catalog:ats-scrapers", priority=Priority.SOURCE)
    rows = {r.name: r for r in DashboardQueries(read_only_conn(conn)).sources()}
    assert set(rows) == {"feed:remotive", "catalog:ats-scrapers"}
    assert rows["catalog:ats-scrapers"].last_checked_at is None
    assert rows["catalog:ats-scrapers"].queued_at is not None
    assert rows["feed:remotive"].queued_at is not None and not rows["feed:remotive"].running


def test_sources_page_shows_every_source_site_and_company_career_page(
    client: TestClient, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock, queue: TaskQueue
) -> None:
    states = SourceStateStore(conn, clock=clock)
    states.record_success("boards:linkedin+indeed")
    states.record_success("search:freehire")
    for _ in range(4):
        states.record_failure(
            "catalog:ats-scrapers", "RetryableError: catalog: the snapshot query took too long"
        )
    queue.enqueue("source_check", "feed:remotive", priority=Priority.SOURCE)  # planned, never read yet
    save(job_store, "linkedin", "Initech", 3)
    save(job_store, "naukri", "Umbrella Softworks", 2)
    save(job_store, "freehire", "Hooli Systems", 6)
    save(job_store, "greenhouse", "Acme Labs", 2)
    save(job_store, "keka", "Vandelay Tech", 1)

    page = client.get("/sources").text
    assert "of 4 read" in page
    assert "Supported hiring systems" in page
    assert "Not read yet" in page and "Next read queued" in page
    assert "Failing · 4 in a row" in page
    career = client.get("/sources/companies").text
    assert "has failed 4 times in a row" in career  # why company career pages stopped arriving
    seen = client.get("/sources/sites").text
    for site in ("linkedin", "naukri", "freehire", "greenhouse", "keka"):
        assert f"<b>{site}</b>" in seen
    assert "Acme Labs" in career and "Vandelay Tech" in career
    assert "Initech" not in career  # a job board, not a company's own career page
    assert "<meter" in seen and "style=" not in seen  # bars without inline styles (the CSP forbids them)
    assert '<td data-label="Jobs" class="num">5</td>' in page  # the job-board search: 3 LinkedIn + 2 Naukri


def test_career_pages_are_paged(client: TestClient, job_store: JobStore) -> None:
    for n in range(PAGE_SIZE + 3):
        save(job_store, "lever", f"Example Company {n:02d}", 1)
    first = client.get("/sources/companies").text
    assert "Example Company 00" in first and f"Example Company {PAGE_SIZE + 2:02d}" not in first
    assert 'href="/sources/companies?page=2"' in first
    second = client.get("/sources/companies?page=2").text
    assert f"Example Company {PAGE_SIZE + 2:02d}" in second and "Example Company 00" not in second
    assert client.get("/sources/companies?page=99").text.count("Example Company") == 3  # the last real page
