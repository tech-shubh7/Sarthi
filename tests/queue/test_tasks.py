# Ported from JobRadar (MIT), tests/test_queue.py @ fdd739b, plus Sarthi's own cases.
import random
from collections.abc import Callable

import pytest

from sarthi.db.tasks import TaskRecord, TaskStatus, TaskStore
from sarthi.queue.tasks import MAX_ATTEMPTS, Priority, TaskQueue, backoff_seconds
from tests.conftest import FakeClock


def test_backoff_schedule_and_cap() -> None:
    rng = random.Random(1)
    for attempt, base in [(1, 30), (2, 120), (3, 480), (4, 1920), (5, 3600), (9, 3600)]:
        delay = backoff_seconds(attempt, rng)
        assert 0.8 * base <= delay <= 1.2 * base


def test_enqueue_is_idempotent_and_claim_order(queue: TaskQueue, clock: FakeClock) -> None:
    queue.enqueue("fetch", "job-b")
    clock.advance(1)
    queue.enqueue("fetch", "job-a")
    queue.enqueue("fetch", "job-b", {"v": 2})  # merges into the pending row

    first = queue.claim()
    second = queue.claim()
    assert (first and first.key, second and second.key) == ("job-b", "job-a")
    assert first is not None
    assert first.payload == {"v": 2}
    assert first.attempts == 1
    assert queue.claim() is None


def test_run_after_and_type_filter(queue: TaskQueue, clock: FakeClock) -> None:
    queue.enqueue("digest", "2026-11-20/pm", delay=60)
    queue.enqueue("fetch", "j1")
    assert queue.claim(["digest"]) is None  # not due yet
    assert queue.claim(["digest", "alert"]) is None
    task = queue.claim(["fetch"])
    assert task is not None
    assert task.key == "j1"
    clock.advance(61)
    task = queue.claim(["digest"])
    assert task is not None
    assert task.key == "2026-11-20/pm"


def test_done_task_can_be_enqueued_again(queue: TaskQueue, task_store: TaskStore) -> None:
    queue.enqueue("alert", "j1")
    task = queue.claim()
    assert task is not None
    queue.complete(task)
    assert task_store.task_counts()[TaskStatus.DONE] == 1

    queue.enqueue("alert", "j1")
    again = queue.claim()
    assert again is not None
    assert (again.id, again.attempts) == (task.id, 1)


def test_enqueue_while_running_runs_again(queue: TaskQueue) -> None:
    queue.enqueue("alert", "j1", {"v": 1})
    task = queue.claim()
    assert task is not None
    queue.enqueue("alert", "j1", {"v": 2})  # job changed while it was being handled
    assert queue.claim() is None  # never two attempts of one task at once
    queue.complete(task)
    again = queue.claim()
    assert again is not None
    assert again.payload == {"v": 2}


def test_retry_with_backoff_then_failed(queue: TaskQueue, task_store: TaskStore, clock: FakeClock) -> None:
    queue.enqueue("fetch", "j1")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        task = queue.claim()
        assert task is not None
        assert task.attempts == attempt
        assert queue.retry(task, "timeout") is (attempt < MAX_ATTEMPTS)
        assert queue.claim() is None  # backoff: not immediately due
        clock.advance(3600 * 1.2 + 1)
    assert queue.claim() is None
    assert task_store.task_counts()[TaskStatus.FAILED] == 1

    assert queue.requeue_failed() == 1
    task = queue.claim()
    assert task is not None
    assert task.attempts == 1


def test_retry_after_is_honoured(queue: TaskQueue, clock: FakeClock) -> None:
    queue.enqueue("fetch", "j1")
    task = queue.claim()
    assert task is not None
    queue.retry(task, "429", retry_after=5)
    clock.advance(4)
    assert queue.claim() is None
    clock.advance(2)
    assert queue.claim() is not None


def test_permanent_fail(queue: TaskQueue, task_store: TaskStore) -> None:
    queue.enqueue("fetch", "j1")
    task = queue.claim()
    assert task is not None
    queue.fail(task, "404")
    assert task_store.task_counts()[TaskStatus.FAILED] == 1


def test_defer_does_not_count_an_attempt(queue: TaskQueue, clock: FakeClock) -> None:
    queue.enqueue("board_check", "linkedin")
    task = queue.claim()
    assert task is not None
    assert task.attempts == 1
    queue.defer(task, 30)
    assert queue.claim() is None
    clock.advance(31)
    again = queue.claim()
    assert again is not None
    assert again.attempts == 1


def test_reclaim_stuck_after_crash(queue: TaskQueue, clock: FakeClock) -> None:
    queue.enqueue("fetch", "j1")
    assert queue.claim() is not None  # then the process "crashes"
    clock.advance(5 * 60)
    assert queue.reclaim_stuck() == 0
    clock.advance(6 * 60)
    assert queue.reclaim_stuck() == 1
    task = queue.claim()
    assert task is not None
    assert task.attempts == 2


REPORTS: dict[str, Callable[[TaskQueue, TaskRecord], object]] = {
    "complete": lambda q, t: q.complete(t),
    "retry": lambda q, t: q.retry(t, "late error"),
    "fail": lambda q, t: q.fail(t, "late error"),
    "defer": lambda q, t: q.defer(t, 1),
}


@pytest.mark.parametrize("report", REPORTS)
def test_second_report_for_an_attempt_is_ignored(queue: TaskQueue, report: str) -> None:
    queue.enqueue("fetch", "j1")
    task = queue.claim()
    assert task is not None
    queue.complete(task)
    REPORTS[report](queue, task)  # e.g. a handler that reports twice
    assert queue.claim() is None


def test_alerts_jump_the_queue_and_priority_never_drops(queue: TaskQueue, clock: FakeClock) -> None:
    for n in range(3):
        queue.enqueue("match", f"old{n}", priority=Priority.BACKGROUND)
    clock.advance(1)
    queue.enqueue("match", "new", priority=Priority.ALERT)
    queue.enqueue("match", "old0", priority=Priority.NORMAL)  # rises from BACKGROUND to NORMAL
    queue.enqueue("match", "new", priority=Priority.BACKGROUND)  # never lowered
    keys = [task.key for task in iter(queue.claim, None)]
    assert keys == ["new", "old0", "old1", "old2"]


def test_secrets_in_errors_are_never_stored(queue: TaskQueue, conn_rows: Callable[[], list[str]]) -> None:
    queue.enqueue("fetch", "j1")
    task = queue.claim()
    assert task is not None
    queue.retry(task, "HTTP 401 with Authorization: Bearer abcdefghijklmnop123")
    assert all("abcdefghijklmnop123" not in error for error in conn_rows())


@pytest.fixture
def conn_rows(task_store: TaskStore) -> Callable[[], list[str]]:
    def errors() -> list[str]:
        rows = task_store._conn.execute("SELECT last_error FROM task WHERE last_error IS NOT NULL")
        return [row["last_error"] for row in rows]

    return errors
