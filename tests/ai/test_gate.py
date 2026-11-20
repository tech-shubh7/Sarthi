import asyncio

import pytest

from sarthi.ai.gate import PriorityGate, local_gate


async def test_one_at_a_time_and_urgent_first() -> None:
    gate = PriorityGate()
    order: list[str] = []
    started = asyncio.Event()

    async def run(name: str, urgent: bool = False) -> None:
        async with gate.hold(urgent=urgent):
            order.append(name)
            if name == "first":
                started.set()
            await asyncio.sleep(0.01)

    first = asyncio.create_task(run("first"))
    await started.wait()
    background = [asyncio.create_task(run(f"job{i}")) for i in range(3)]
    await asyncio.sleep(0)
    chat = asyncio.create_task(run("chat", urgent=True))
    await asyncio.gather(first, chat, *background)
    assert order == ["first", "chat", "job0", "job1", "job2"]


async def test_a_cancelled_waiter_does_not_block_the_gate() -> None:
    gate = PriorityGate()
    release = asyncio.Event()

    async def holder() -> None:
        async with gate.hold():
            await release.wait()

    async def waiter() -> None:
        async with gate.hold():
            pass

    held = asyncio.create_task(holder())
    await asyncio.sleep(0)
    cancelled = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    cancelled.cancel()
    release.set()
    await held
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    async with asyncio.timeout(1):
        async with gate.hold(urgent=True):
            pass


def test_one_gate_per_local_server() -> None:
    assert local_gate("http://localhost:11434/v1") is local_gate("http://localhost:11434/v1")
    assert local_gate("http://localhost:11434/v1") is not local_gate("http://other:11434/v1")
