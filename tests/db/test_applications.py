"""Which handed-over applications a new site login can retry."""

import sqlite3

import pytest

from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.jobs import JobStore
from tests.matching.conftest import make_job


def test_jobs_waiting_for_a_login_are_found_by_site(conn: sqlite3.Connection, job_store: JobStore) -> None:
    store = ApplicationStore(conn)
    cases = {
        "a": ("in.indeed.com", "in.indeed.com needs your own logged-in account to apply."),
        "b": ("in.indeed.com", "in.indeed.com shows you as signed out. Log in once with /login."),
        "c": ("in.indeed.com", "The form has a CAPTCHA, which only you can solve."),  # not a login
        "d": ("www.linkedin.com", "LinkedIn needs you to log in once: run `uv run sarthi login linkedin`"),
        "e": ("notindeed.com", "notindeed.com needs your own logged-in account to apply."),  # other site
        "f": ("indeed.com", "indeed.com needs your own logged-in account to apply."),
    }
    ids = {}
    for key, (site, detail) in cases.items():
        job = make_job(url=f"https://jobs.example.org/{key}", company=f"Company {key.upper()}")
        job_id = job_store.save(job).job_id
        store.record(job_id, ApplicationStatus.NEEDS_HUMAN, site=site, detail=detail)
        ids[key] = job_id
    sent = job_store.save(make_job(url="https://jobs.example.org/g", company="Company G")).job_id
    assert len({*ids.values(), sent}) == len(cases) + 1
    store.record(sent, ApplicationStatus.SUBMITTED, site="in.indeed.com", detail="logged-in account")
    assert set(store.waiting_for_login("indeed.com")) == {ids["a"], ids["b"], ids["f"]}
    assert store.waiting_for_login("linkedin.com") == [ids["d"]]
    assert store.waiting_for_login("in_eed.com") == []  # LIKE wildcards are literal


@pytest.mark.parametrize("sent", [ApplicationStatus.SUBMITTED, ApplicationStatus.APPLIED_BY_USER])
@pytest.mark.parametrize(
    "later", [ApplicationStatus.FILLED, ApplicationStatus.NEEDS_HUMAN, ApplicationStatus.FAILED]
)
def test_a_sent_application_never_moves_back(
    conn: sqlite3.Connection, job_store: JobStore, sent: ApplicationStatus, later: ApplicationStatus
) -> None:
    store = ApplicationStore(conn)
    job = job_store.save(make_job(url="https://jobs.example.com/1")).job_id
    store.record(job, sent, site="jobs.example.com", detail="sent")
    store.record(job, later, detail="a late refill")
    record = store.get(job)
    assert record is not None
    assert record.status is sent
    assert record.detail == "sent"
    assert store.was_sent(job)
    store.record(job, ApplicationStatus.APPLIED_BY_USER, detail="the user said so")
    assert store.was_sent(job)
    assert not store.was_sent("job-other")
