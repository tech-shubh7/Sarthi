import asyncio
import os
import signal
import sys

import pytest

from sarthi.cli import until_terminated


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGTERM handlers on Windows event loops")
def test_sigterm_cancels_the_run_so_workers_hand_tasks_back() -> None:
    handed_back: list[str] = []

    async def run() -> None:
        asyncio.get_running_loop().call_later(0.05, os.kill, os.getpid(), signal.SIGTERM)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            handed_back.append("task")  # a worker's defer(task, 0)
            raise

    asyncio.run(asyncio.wait_for(until_terminated(run()), timeout=5))
    assert handed_back == ["task"]
