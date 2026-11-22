"""Stopping Sarthi while a catalog query runs must stop the query too, not wait minutes for it."""

import asyncio
import time

import pytest

from sarthi.queue.tasks import RetryableError
from sarthi.sources import catalog

SLOW = "SELECT count(*) AS n FROM range(?) a, range(?) b"  # seconds of work for DuckDB


async def test_a_cancelled_query_is_interrupted() -> None:
    task = asyncio.create_task(catalog._run(SLOW, [200_000, 200_000]))
    await asyncio.sleep(0.3)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 5  # interrupted, not run to the end


async def test_a_query_over_time_is_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "QUERY_TIMEOUT_S", 0.3)
    started = time.monotonic()
    with pytest.raises(RetryableError, match="took too long"):
        await catalog._run(SLOW, [200_000, 200_000])
    assert time.monotonic() - started < 5


async def test_a_quick_query_still_answers() -> None:
    assert await catalog._run("SELECT 1 + ? AS n", [41]) == [{"n": 42}]
