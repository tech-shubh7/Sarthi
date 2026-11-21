from typing import Any

import pytest

from sarthi.db.jobs import JobStore
from sarthi.db.source_state import SourceStateStore
from sarthi.db.tasks import TaskStore
from sarthi.jobs.models import JobPosting
from sarthi.queue.tasks import Priority, RetryableError, TaskQueue
from sarthi.sources.base import SearchQuery
from sarthi.sources.ingest import MATCH_TASK, Ingestor
from tests.conftest import FakeClock


def posting(n: int, **overrides: Any) -> JobPosting:
    data: dict[str, Any] = {
        "source": "greenhouse",
        "url": f"https://boards.greenhouse.io/acme/jobs/{n}",
        "title": f"Engineer {n}",
        "company": "Acme",
        "location_text": "Bengaluru, India",
        "description": "word " * 400,
    }
    return JobPosting.model_validate(data | overrides)


class FakeSource:
    name = "greenhouse:acme"

    def __init__(self) -> None:
        self.jobs: list[JobPosting] = []
        self.error: Exception | None = None

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        if self.error:
            raise self.error
        return self.jobs


@pytest.fixture
def states(conn: Any, clock: FakeClock) -> SourceStateStore:
    return SourceStateStore(conn, clock=clock)


@pytest.fixture
def ingestor(job_store: JobStore, queue: TaskQueue, states: SourceStateStore) -> Ingestor:
    return Ingestor(job_store, queue, states)


async def test_first_read_is_a_quiet_baseline_then_new_jobs_alert(
    ingestor: Ingestor, task_store: TaskStore, job_store: JobStore
) -> None:
    source = FakeSource()
    source.jobs = [posting(1), posting(2)]
    first = await ingestor.run(source, SearchQuery())
    assert (first.fetched, first.new, first.queued, first.baseline) == (2, 2, 0, True)

    source.jobs = [posting(1), posting(2), posting(3)]
    second = await ingestor.run(source, SearchQuery())
    assert (second.fetched, second.new, second.queued, second.baseline) == (3, 1, 1, False)

    task = ingestor.queue.claim()
    assert task is not None
    assert task.type == MATCH_TASK
    assert job_store.get(task.key) is not None


async def test_stored_jobs_are_completed_and_lean(ingestor: Ingestor, job_store: JobStore) -> None:
    source = FakeSource()
    source.jobs = [posting(1)]
    await ingestor.run(source, SearchQuery(), alert=True)
    task = ingestor.queue.claim()
    assert task is not None
    stored = job_store.get(task.key)
    assert stored is not None
    assert stored.country_codes == {"IN"}  # filled from "Bengaluru, India"
    assert not stored.description_complete
    assert len(stored.description) <= 701


async def test_jobs_without_their_full_text_have_their_page_read_first(
    job_store: JobStore, queue: TaskQueue, states: SourceStateStore
) -> None:
    filling = Ingestor(job_store, queue, states, fill_text=True)
    source = FakeSource()
    source.jobs = [posting(1, description="Snippet.", description_complete=False), posting(2)]
    await filling.run(source, SearchQuery(), alert=True)
    types = sorted(t.type for t in iter(queue.claim, None))
    assert types == ["fill_text", MATCH_TASK]  # the full description (posting 2) goes straight to matching
    snippet_only = job_store.get(job_store.save(posting(1, description="Snippet.")).job_id)
    assert snippet_only is not None
    assert not snippet_only.description_complete  # a source's snippet stays marked a snippet


async def test_without_filling_every_job_is_matched_directly(ingestor: Ingestor) -> None:
    source = FakeSource()
    source.jobs = [posting(1, description="Snippet.", description_complete=False)]
    await ingestor.run(source, SearchQuery(), alert=True)
    task = ingestor.queue.claim()
    assert task is not None
    assert task.type == MATCH_TASK


async def test_catch_up_alerts_even_on_the_first_read(ingestor: Ingestor) -> None:
    source = FakeSource()
    source.jobs = [posting(1)]
    report = await ingestor.run(source, SearchQuery(hours_old=48), alert=True)
    assert (report.queued, report.baseline) == (1, False)
    claimed = ingestor.queue.claim()
    assert claimed is not None


async def test_alerts_outrank_background_work(ingestor: Ingestor) -> None:
    ingestor.queue.enqueue("tailor", "old", priority=Priority.BACKGROUND)
    source = FakeSource()
    source.jobs = [posting(1)]
    await ingestor.run(source, SearchQuery(), alert=True)
    first = ingestor.queue.claim()
    assert first is not None
    assert first.type == MATCH_TASK


async def test_failures_are_tracked_and_re_raised(ingestor: Ingestor, states: SourceStateStore) -> None:
    source = FakeSource()
    source.error = RetryableError("HTTP 503, token=abc123secret")
    for streak in (1, 2):
        with pytest.raises(RetryableError):
            await ingestor.run(source, SearchQuery())
        state = states.get(source.name)
        assert state is not None
        assert state.consecutive_failures == streak
        assert not state.baseline_done
        assert "abc123secret" not in (state.last_error or "")

    source.error = None
    await ingestor.run(source, SearchQuery())
    state = states.get(source.name)
    assert state is not None
    assert (state.consecutive_failures, state.last_error, state.baseline_done) == (0, None, True)


async def test_an_unusable_posting_does_not_drop_the_batch(ingestor: Ingestor) -> None:
    source = FakeSource()
    source.jobs = [posting(1, url="https://localhost/jobs/1"), posting(2)]
    report = await ingestor.run(source, SearchQuery(), alert=True)
    assert (report.fetched, report.new) == (2, 1)


def test_unknown_source_has_no_state(states: SourceStateStore) -> None:
    assert states.get("never-seen") is None
