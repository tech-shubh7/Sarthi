"""Turning a burst of chat messages into one, and handling them one at a time without blocking the bot.

People often send one thought as several quick messages ("remove fintech" ... "and add ML engineer").
The inbox waits briefly after the last message and hands the whole burst over at once. While a burst is
being handled (the AI can take a while), new messages are kept for the next round instead of being read as
answers to a question the user has not seen yet; the user gets one short "still working" note. Polling
never waits for this.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

Message = dict[str, Any]
BatchHandler = Callable[[int, list[Message]], Awaitable[None]]
BusyNotice = Callable[[int], Awaitable[None]]

QUIET_SECONDS = 2.0


@dataclass
class _Chat:
    pending: list[Message] = field(default_factory=list)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task[None] | None = None
    handling: bool = False
    notified: bool = False


class Inbox:
    """Per-chat message buffer with a short quiet period and one batch in flight at a time."""

    def __init__(
        self, handle: BatchHandler, busy_notice: BusyNotice, quiet_seconds: float = QUIET_SECONDS
    ) -> None:
        """Call ``handle`` with each batch; call ``busy_notice`` once when messages arrive mid-batch."""
        self._handle = handle
        self._busy_notice = busy_notice
        self._quiet = quiet_seconds
        self._chats: dict[int, _Chat] = {}

    @property
    def busy(self) -> bool:
        """True while any chat's messages are being handled (e.g. the AI is reading an answer)."""
        return any(chat.handling for chat in self._chats.values())

    async def put(self, chat_id: int, message: Message) -> None:
        """Queue a message; returns at once."""
        chat = self._chats.setdefault(chat_id, _Chat())
        chat.pending.append(message)
        chat.arrived.set()
        if chat.handling and not chat.notified:
            chat.notified = True
            await self._busy_notice(chat_id)
        if chat.worker is None or chat.worker.done():
            chat.worker = asyncio.create_task(self._drain(chat_id, chat), name=f"inbox:{chat_id}")

    async def _wait_for_quiet(self, chat: _Chat) -> None:
        while True:
            chat.arrived.clear()
            try:
                await asyncio.wait_for(chat.arrived.wait(), timeout=self._quiet)
            except TimeoutError:
                return

    async def _drain(self, chat_id: int, chat: _Chat) -> None:
        while chat.pending:
            await self._wait_for_quiet(chat)
            batch, chat.pending = chat.pending, []
            chat.handling, chat.notified = True, False
            try:
                await self._handle(chat_id, batch)
            except Exception:  # one bad batch must not stop the chat; the user can just send again
                log.exception("chat %s: could not handle %d message(s)", chat_id, len(batch))
            finally:
                chat.handling = False

    async def close(self) -> None:
        """Stop all chat workers (on shutdown)."""
        for chat in self._chats.values():
            if chat.worker is not None:
                chat.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await chat.worker
