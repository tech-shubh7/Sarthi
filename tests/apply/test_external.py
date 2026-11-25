"""Board jobs that apply on the company's own site: the address is read and the application goes on there.

Local pages shaped like LinkedIn's and Naukri's (fictional company, no network: every web request in the
browser is answered from tests/apply/forms or refused).
"""

import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from sarthi.apply import linkedin, naukri
from sarthi.apply.engine import ApplyEngine, FillResult
from sarthi.apply.external import ExternalApplyError, unwrap
from sarthi.apply.handler import external_refusal
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.jobs import net
from sarthi.queue import names
from sarthi.queue.tasks import DeferError, TaskQueue
from tests.apply.test_handler import FILLED, FakeEngine, applier, job_at, payload
from tests.conftest import FakeClock

FORMS = Path(__file__).parent / "forms"
LINKEDIN_JOB = "https://www.linkedin.com/jobs/view/7"
NAUKRI_JOB = "https://www.naukri.com/job-listings-backend-engineer-acme-example-labs-7"
COMPANY_FORM = "https://careers.acme.example/jobs/7/apply"
PUBLIC_IP = "93.184.215.14"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names resolve to a public address without a real lookup (literal private IPs stay private)."""

    async def resolve(host: str) -> set[str]:
        return {PUBLIC_IP}

    monkeypatch.setattr(net, "resolve", resolve)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[ApplyEngine]:
    made = ApplyEngine(tmp_path / "shots", url_check=None)  # local test pages
    try:
        first = await made._new_page()
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"Chromium not installed for Playwright: {error}")
    await first.close()
    yield made
    await made.aclose()


async def serve(engine: ApplyEngine, pages: dict[str, str]) -> list[str]:
    """Answer web requests for ``pages`` (host -> local file), refuse all others; returns what was asked."""
    seen: list[str] = []

    async def answer(route: Any) -> None:
        seen.append(route.request.url)
        name = pages.get(urlparse(route.request.url).hostname or "")
        if name is None:
            await route.abort()
        else:
            await route.fulfill(path=FORMS / name, content_type="text/html")

    assert engine._context is not None
    await engine._context.route(re.compile(r"^https?://"), answer)
    return seen


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (
            "https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fcareers.acme.example%2Fjobs%2F7&urlhash=x",
            "https://careers.acme.example/jobs/7",
        ),
        ("https://www.linkedin.com/safety/go?url=https%3A%2F%2Facme.example%2Fa", "https://acme.example/a"),
        (
            "https://www.linkedin.com/redir/redirect?url=javascript%3Aalert(1)",
            "https://www.linkedin.com/redir/redirect?url=javascript%3Aalert(1)",
        ),
        (
            "https://acme.example/apply?url=https%3A%2F%2Fother.example",
            "https://acme.example/apply?url=https%3A%2F%2Fother.example",
        ),
    ],
)
def test_unwrap_reads_only_the_boards_own_redirects(given: str, expected: str) -> None:
    assert unwrap(given, linkedin.is_linkedin) == expected


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("https://careers.acme.example/jobs/7", ""),
        ("http://127.0.0.1:8080/admin", "not a public web address"),
        ("http://192.168.1.1/", "not a public web address"),
        ("http://localhost/apply", "not a public web address"),
        ("file:///etc/passwd", "not a public web address"),
        ("javascript:alert(1)", "not a public web address"),
        ("https://user:secret@acme.example/apply", "not a public web address"),
        ("https://www.linkedin.com/jobs/view/8", "back to a job board"),
        ("https://www.naukri.com/job-listings-x-8", "back to a job board"),
    ],
)
async def test_only_public_web_pages_are_opened(url: str, why: str) -> None:
    refusal = await external_refusal(url)
    assert (why in refusal) if why else refusal == ""


async def test_linkedin_apply_button_through_its_dialog_and_redirect(engine: ApplyEngine) -> None:
    seen = await serve(engine, {"www.linkedin.com": "thanks.html"})
    page = await engine._new_page()
    with pytest.raises(ExternalApplyError) as raised:
        await linkedin.open_easy_apply(page, (FORMS / "linkedin_company_site.html").as_uri())
    assert raised.value.url == COMPANY_FORM  # read from LinkedIn's redirect, before the company page loads
    assert page.context.pages == [page]  # the tab the button opened is closed again
    assert "careers.acme.example" not in {urlparse(u).hostname for u in seen}  # opened only once checked


async def test_naukri_company_site_button(engine: ApplyEngine) -> None:
    await serve(engine, {"careers.acme.example": "company_apply.html"})
    page = await engine._new_page()
    with pytest.raises(ExternalApplyError) as raised:
        await naukri.open_job(page, (FORMS / "naukri_company.html").as_uri())
    assert raised.value.url == COMPANY_FORM
    assert page.context.pages == [page]


@pytest.mark.parametrize(
    ("job_url", "pages", "form_url"),
    [
        (
            LINKEDIN_JOB,
            {"www.linkedin.com": "linkedin_external.html", "acme.example": "company_apply.html"},
            "https://acme.example/careers",
        ),
        (
            NAUKRI_JOB,
            {"www.naukri.com": "naukri_company.html", "careers.acme.example": "company_apply.html"},
            COMPANY_FORM,
        ),
    ],
)
async def test_a_board_job_is_filled_on_the_company_site_and_waits_for_approval(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
    engine: ApplyEngine,
    job_url: str,
    pages: dict[str, str],
    form_url: str,
) -> None:
    await serve(engine, pages)
    job_id = job_at(job_store, job_url)
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(  # type: ignore[arg-type]
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    preview = payload(queue, names.APPLY_PREVIEW)  # nothing is sent before the user's approval
    assert preview["system"] == "generic"
    assert engine._open[job_id].url == form_url  # the form held open is the company's own
    record = ApplicationStore(conn).get(job_id)
    assert record is not None
    assert (record.status, record.site) == (ApplicationStatus.FILLED, urlparse(form_url).hostname)


async def test_an_unsafe_company_link_is_refused_and_handed_over(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
    engine: ApplyEngine,
) -> None:
    seen = await serve(engine, {"www.linkedin.com": "linkedin_unsafe.html"})
    job_id = job_at(job_store, LINKEDIN_JOB)
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(  # type: ignore[arg-type]
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    result = payload(queue, names.APPLY_RESULT)
    assert result["outcome"] == "needs_human"
    assert "not a public web address" in result["detail"]
    assert result["url"] == LINKEDIN_JOB  # the user gets the job page, not the refused link
    assert seen == [LINKEDIN_JOB]  # the private address was never requested
    assert job_id not in engine._open
    record = ApplicationStore(conn).get(job_id)
    assert record is not None
    assert (record.status, record.site) == (ApplicationStatus.NEEDS_HUMAN, "www.linkedin.com")


class CompanySiteEngine(FakeEngine):
    """A board job whose page leads to ``target``; that page then fills as ``result``."""

    def __init__(self, target: str, result: FillResult | Exception = FILLED) -> None:
        super().__init__(result)
        self.target = target

    async def fill(self, job_id: str, url: str, ctx: Any, *, one_click: bool = False) -> FillResult:
        if linkedin.is_linkedin(url) or naukri.is_naukri(url):
            self.filled.append(url)
            raise ExternalApplyError(self.target)
        return await super().fill(job_id, url, ctx, one_click=one_click)


async def test_the_company_sites_daily_limit_applies(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    store = ApplicationStore(conn, clock=clock)
    done = job_at(job_store, "https://careers.acme.example/jobs/1", title="Data Engineer")
    store.record(done, ApplicationStatus.SUBMITTED, site="careers.acme.example")
    engine = CompanySiteEngine(COMPANY_FORM)
    job_id = job_at(job_store, LINKEDIN_JOB)
    with pytest.raises(DeferError):
        await applier(conn, job_store, queue, tmp_path, engine, clock, limit=1).handle_apply(
            TaskRecord(1, names.APPLY, job_id, {}, 1)
        )
    assert engine.filled == [LINKEDIN_JOB]  # the company's form was not opened


@pytest.mark.parametrize(
    ("target", "why"),
    [
        ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/7", "Workday"),
        ("https://in.indeed.com/viewjob?jk=7", "logged-in account"),
        ("https://www.naukri.com/job-listings-x-7", "back to a job board"),
    ],
)
async def test_company_sites_that_need_a_person_are_handed_over(
    conn: sqlite3.Connection,
    job_store: JobStore,
    queue: TaskQueue,
    tmp_path: Path,
    clock: FakeClock,
    target: str,
    why: str,
) -> None:
    engine = CompanySiteEngine(target)
    job_id = job_at(job_store, LINKEDIN_JOB)
    await applier(conn, job_store, queue, tmp_path, engine, clock).handle_apply(
        TaskRecord(1, names.APPLY, job_id, {}, 1)
    )
    assert why in payload(queue, names.APPLY_RESULT)["detail"]
    assert engine.filled == [LINKEDIN_JOB]


async def test_the_submission_is_recorded_for_the_company_site(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    engine = CompanySiteEngine(COMPANY_FORM)
    job_id = job_at(job_store, LINKEDIN_JOB)
    flow = applier(conn, job_store, queue, tmp_path, engine, clock)
    await flow.handle_apply(TaskRecord(1, names.APPLY, job_id, {}, 1))
    assert engine.filled == [LINKEDIN_JOB, COMPANY_FORM]
    assert engine.one_click == [False]  # a company site, not a job board: its Submit waits for approval
    await flow.handle_submit(TaskRecord(2, names.SUBMIT, job_id, {"code": "ABC234"}, 1))
    record = ApplicationStore(conn, clock=clock).get(job_id)
    assert record is not None
    assert (record.status, record.site) == (ApplicationStatus.SUBMITTED, "careers.acme.example")
