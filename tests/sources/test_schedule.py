import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from sarthi.db.jobs import JobStore
from sarthi.db.source_state import SourceState, SourceStateStore
from sarthi.db.tasks import TaskRecord, TaskStatus, TaskStore
from sarthi.jobs.models import JobPosting
from sarthi.profile.models import Profile
from sarthi.queue.tasks import PermanentError, RetryableError, TaskQueue
from sarthi.sources.base import SearchQuery
from sarthi.sources.ingest import Ingestor
from sarthi.sources.schedule import (
    MAX_LOOK_BACK_HOURS,
    Planned,
    SourceScheduler,
    look_back_hours,
    plan_sources,
    query_for,
)
from tests.conftest import FakeClock
from tests.matching.conftest import NOW, make_job, make_profile

HOUR = timedelta(hours=1)


def state(last_ok: str | None, last_checked: str | None = None, failures: int = 0) -> SourceState:
    return SourceState("s", last_ok is not None, last_checked or last_ok, last_ok, failures, None)


def test_plan_sources_skips_bad_entries() -> None:
    planned = plan_sources(
        ["linkedin"], ["greenhouse/razorpay", "Lever/acme", "nonsense", "/x"], HOUR, HOUR / 2
    )
    assert [p.source.name for p in planned] == ["boards:linkedin", "greenhouse:razorpay", "lever:acme"]
    assert [p.kind for p in planned] == ["boards", "company", "company"]
    assert plan_sources([], [], HOUR, HOUR) == []


def test_look_back_covers_the_gap_since_the_last_success() -> None:
    assert look_back_hours("boards", None, NOW) == 24
    assert look_back_hours("company", None, NOW) == 72
    assert look_back_hours("boards", state("2026-11-20T08:30:00.000000Z"), NOW) == 2
    assert (
        look_back_hours("boards", state("2026-11-18T09:00:00.000000Z"), NOW) == 49
    )  # laptop off for two days
    assert look_back_hours("boards", state("2026-01-01T00:00:00.000000Z"), NOW) == MAX_LOOK_BACK_HOURS


def test_query_uses_the_profile() -> None:
    profile = make_profile(countries=["GB"])
    query = query_for(profile, "boards", 5)
    assert query == SearchQuery(
        roles=("Backend Engineer",), countries=("GB", "IN"), remote=False, hours_old=5
    )
    remote_only = query_for(make_profile(remote="only"), "boards", 5)
    assert remote_only is not None
    assert (remote_only.countries, remote_only.remote) == ((), True)
    assert query_for(None, "boards", 5) is None
    assert query_for(None, "company", 72) == SearchQuery(hours_old=72)


class FakeSource:
    def __init__(self, name: str, result: list[JobPosting] | Exception) -> None:
        self.name = name
        self.result = result
        self.queries: list[SearchQuery] = []

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        self.queries.append(query)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def make_scheduler(conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock) -> Any:
    def build(sources: list[tuple[FakeSource, str]], profile: Profile | None) -> SourceScheduler:
        states = SourceStateStore(conn, clock=clock)
        planned = [Planned(source, kind, HOUR) for source, kind in sources]  # type: ignore[arg-type]
        return SourceScheduler(
            planned, Ingestor(job_store, queue, states), states, queue, lambda: profile, clock=clock
        )

    return build


def pending(store: TaskStore) -> int:
    return store.task_counts()[TaskStatus.PENDING]


async def test_tick_queues_due_sources_and_waits_for_a_profile(
    make_scheduler: Any, task_store: TaskStore
) -> None:
    boards, company = FakeSource("boards:linkedin", []), FakeSource("greenhouse:acme", [])
    without_profile = make_scheduler([(boards, "boards"), (company, "company")], None)
    assert without_profile.tick() == 1  # only the company board: searches need roles
    with_profile = make_scheduler([(boards, "boards"), (company, "company")], make_profile())
    assert with_profile.tick() == 2
    assert pending(task_store) == 2


async def test_a_checked_source_waits_its_interval(
    make_scheduler: Any, clock: FakeClock, task_store: TaskStore
) -> None:
    source = FakeSource("greenhouse:acme", [make_job()])
    scheduler = make_scheduler([(source, "company")], make_profile())
    await scheduler.handle(TaskRecord(1, "source_check", source.name, {}, 1))
    assert source.queries[0].hours_old == 72
    assert pending(task_store) == 1  # the new job's match task
    assert scheduler.tick() == 0
    clock.advance(3600)
    assert scheduler.tick() == 1


async def test_failures_back_off_and_never_raise(make_scheduler: Any, clock: FakeClock) -> None:
    source = FakeSource("greenhouse:acme", RetryableError("HTTP 503"))
    scheduler = make_scheduler([(source, "company")], None)
    for _ in range(3):
        await scheduler.handle(TaskRecord(1, "source_check", source.name, {}, 1))  # recorded, not raised
    clock.advance(4 * 3600)
    assert scheduler.tick() == 0  # third failure: waits 8x the interval
    clock.advance(4 * 3600 + 1)
    assert scheduler.tick() == 1
    source.result = PermanentError("company board not found")
    await scheduler.handle(TaskRecord(1, "source_check", source.name, {}, 1))


async def test_board_check_without_profile_or_unknown_source_does_nothing(make_scheduler: Any) -> None:
    boards = FakeSource("boards:linkedin", [])
    scheduler = make_scheduler([(boards, "boards")], None)
    await scheduler.handle(TaskRecord(1, "source_check", boards.name, {}, 1))
    await scheduler.handle(TaskRecord(1, "source_check", "removed:source", {}, 1))
    assert boards.queries == []
