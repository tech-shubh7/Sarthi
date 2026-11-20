"""One AI request at a time on a local model, with the user's chat going first.

A local model on a laptop CPU answers one request at a time; sending several at once only makes each one slow
and can time out. The gate lets one request through per local server, and anything the user is waiting on
(``urgent``) jumps ahead of waiting background work such as job scoring.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator


class PriorityGate:
    """A lock where urgent waiters are served before normal ones (first come, first served within each)."""

    def __init__(self) -> None:
        """Start unlocked."""
        self._busy = False
        self._urgent: list[asyncio.Future[None]] = []
        self._normal: list[asyncio.Future[None]] = []

    def _wake_next(self) -> None:
        for queue in (self._urgent, self._normal):
            while queue:
                waiter = queue.pop(0)
                if not waiter.done():
                    waiter.set_result(None)
                    return
        self._busy = False

    @contextlib.asynccontextmanager
    async def hold(self, *, urgent: bool = False) -> AsyncIterator[None]:
        """Wait for a turn, run the block, then pass the turn on."""
        if self._busy:
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            (self._urgent if urgent else self._normal).append(waiter)
            try:
                await waiter
            except asyncio.CancelledError:
                if waiter.done() and not waiter.cancelled():
                    self._wake_next()  # we were handed the turn but will not use it
                raise
        else:
            self._busy = True
        try:
            yield
        finally:
            self._wake_next()


_LOCAL_GATES: dict[str, PriorityGate] = {}


def local_gate(base_url: str) -> PriorityGate:
    """The shared gate for one local AI server (every client in the process uses the same one)."""
    return _LOCAL_GATES.setdefault(base_url, PriorityGate())
