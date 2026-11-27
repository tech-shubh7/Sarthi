"""A site's firewall page is "apply yourself", never "signed out"; no paid documents for a blocked site."""

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from sarthi.apply import adapters
from sarthi.apply.adapters import FillError
from sarthi.apply.agent import Button, LoginWallError, dead_end
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.jobs import JobStore
from sarthi.queue.tasks import TaskQueue
from tests.apply.test_handler import FILLED, FakeEngine, applier, job_at
from tests.conftest import FakeClock

INDEED_BLOCK = (
    "Find jobs Company Reviews Find salaries Upload your resume Sign in Employers / Post Job "
    "Request Blocked You have been blocked. If you believe this in error, please go to support.indeed.com "
    "Your Ray ID for this request is a6134e4dde993b9d Troubleshooting Cloudflare Errors"
)


def test_a_firewall_page_is_not_taken_for_a_login_wall() -> None:
    buttons = [Button("b1", "Sign in", "page"), Button("b2", "Return home", "page")]
    with pytest.raises(FillError, match="blocks automated browsers") as caught:
        dead_end("https://in.indeed.com/viewjob?jk=1", [], buttons, INDEED_BLOCK, one_click=True)
    assert not isinstance(caught.value, LoginWallError)
    assert "signed out" not in str(caught.value)


@pytest.mark.parametrize(
    "text",
    ["Access Denied You don't have permission to access this page", "Performing security verification"],
)
def test_other_firewall_pages(text: str) -> None:
    assert adapters.BLOCKED_PAGE.search(text)


def test_an_ordinary_job_page_is_not_a_firewall_page() -> None:
    assert not adapters.BLOCKED_PAGE.search("Apply now. We have blocked out time for interviews next week.")


def test_no_documents_are_made_for_a_site_whose_firewall_refused_sarthi(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, tmp_path: Path, clock: FakeClock
) -> None:
    store = ApplicationStore(conn, clock=clock)
    blocked = job_at(job_store, "https://in.indeed.com/viewjob?jk=1", company="Earlier")
    store.record(
        blocked,
        ApplicationStatus.NEEDS_HUMAN,
        site="in.indeed.com",
        detail=adapters.blocked_reason("https://in.indeed.com/x"),
    )
    worker = applier(conn, job_store, queue, tmp_path, FakeEngine(FILLED), clock)
    later = job_at(job_store, "https://in.indeed.com/viewjob?jk=2", company="Later", docs=False)
    assert worker.skip_application_documents(later)  # handed over, no paid resume and letter
    record = store.get(later)
    assert record is not None and "blocks automated browsers" in record.detail
    clock.advance(timedelta(days=2).total_seconds())
    other = job_at(job_store, "https://in.indeed.com/viewjob?jk=3", company="Much later", docs=False)
    assert not worker.skip_application_documents(other)  # a day later the site is tried again
