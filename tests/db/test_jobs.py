from typing import Any

import pytest

from sarthi.db.jobs import JobStore
from sarthi.jobs.models import JobPosting
from tests.conftest import FakeClock


def make(**overrides: Any) -> JobPosting:
    data: dict[str, Any] = {
        "source": "greenhouse",
        "url": "https://boards.greenhouse.io/razorpay/jobs/1",
        "title": "Backend Engineer",
        "company": "Razorpay",
        "country_codes": ["IN"],
    }
    return JobPosting.model_validate(data | overrides)


def test_new_job_is_saved_and_read_back(job_store: JobStore) -> None:
    job = make()
    result = job_store.save(job)
    assert result.is_new
    assert job_store.get(result.job_id) == job
    assert job_store.count() == 1
    assert job_store.get("missing") is None


def test_same_link_with_tracking_params_is_the_same_job(job_store: JobStore) -> None:
    first = job_store.save(make())
    again = job_store.save(make(url="https://boards.greenhouse.io/razorpay/jobs/1/?utm_source=x&gh_src=y"))
    assert (again.job_id, again.is_new) == (first.job_id, False)
    assert job_store.sources(first.job_id) == ["https://boards.greenhouse.io/razorpay/jobs/1"]


def test_same_role_on_another_board_joins_the_first_job(job_store: JobStore) -> None:
    first = job_store.save(make())
    linkedin = job_store.save(
        make(source="linkedin", url="https://www.linkedin.com/jobs/view/42", company="Razorpay Pvt Ltd")
    )
    assert (linkedin.job_id, linkedin.is_new) == (first.job_id, False)
    assert len(job_store.sources(first.job_id)) == 2
    assert job_store.count() == 1


def test_same_role_in_another_country_is_a_different_job(job_store: JobStore) -> None:
    india = job_store.save(make())
    uk = job_store.save(make(url="https://boards.greenhouse.io/razorpay/jobs/2", country_codes=["GB"]))
    assert uk.is_new
    assert uk.job_id != india.job_id


def test_unknown_country_joins_a_recent_same_role(job_store: JobStore) -> None:
    first = job_store.save(make())
    unknown = job_store.save(make(source="freehire", url="https://freehire.me/j/9", country_codes=[]))
    assert (unknown.job_id, unknown.is_new) == (first.job_id, False)


def test_an_old_role_reposted_after_the_window_is_new(job_store: JobStore, clock: FakeClock) -> None:
    first = job_store.save(make())
    clock.advance(31 * 24 * 3600)
    later = job_store.save(make(url="https://boards.greenhouse.io/razorpay/jobs/7"))
    assert later.is_new
    assert later.job_id != first.job_id


def test_unusable_link_is_refused(job_store: JobStore) -> None:
    with pytest.raises(ValueError, match="not a usable"):
        job_store.save(make(url="https://localhost/jobs/1"))
