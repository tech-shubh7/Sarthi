"""Builds Sarthi from settings and runs it: the only module that knows every part (composition root)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from sarthi.ai.client import AiClient
from sarthi.ai.providers import resolve
from sarthi.ai.scoring import LlmMatchScorer
from sarthi.chat.telegram.api import TelegramApi
from sarthi.chat.telegram.bot import TelegramBot
from sarthi.clock import Clock, utcnow
from sarthi.config import Settings
from sarthi.db.ai_store import AiStore
from sarthi.db.chat import ChatStore
from sarthi.db.connection import connect
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.tasks import TaskRecord, TaskStore
from sarthi.matching.handler import ALERT_TASK, Matcher
from sarthi.profile.store import load_profile, profile_path
from sarthi.queue.tasks import Priority, TaskQueue
from sarthi.queue.worker import Worker
from sarthi.sources.ingest import MATCH_TASK

log = logging.getLogger(__name__)

DIGEST_TASK = "digest"


class SetupError(Exception):
    """Sarthi cannot start because something is not configured."""


def next_digest_at(now: datetime, hour: int) -> datetime:
    """The next time the local clock shows ``hour``:00 (today if still ahead, else tomorrow)."""
    local = now.astimezone()
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return target if target > local else target + timedelta(days=1)


@dataclass
class App:
    """Everything a running Sarthi needs, wired together."""

    settings: Settings
    conn: sqlite3.Connection
    queue: TaskQueue
    bot: TelegramBot
    matcher: Matcher
    ai: AiClient
    clock: Clock

    def schedule_digest(self) -> None:
        """Queue the next daily digest (idempotent: one task per local date)."""
        at = next_digest_at(self.clock(), self.settings.digest_hour)
        delay = (at - self.clock()).total_seconds()
        self.queue.enqueue(DIGEST_TASK, at.date().isoformat(), delay=delay, priority=Priority.NORMAL)

    async def _digest(self, task: TaskRecord) -> None:
        await self.bot.handle_digest(task)
        self.schedule_digest()

    def worker(self) -> Worker:
        """A worker for matching, alerts and digests."""
        handlers = {
            MATCH_TASK: self.matcher.handle,
            ALERT_TASK: self.bot.handle_alert,
            DIGEST_TASK: self._digest,
        }
        return Worker(self.queue, handlers, concurrency=2, name="sarthi")

    async def run(self) -> None:
        """Run the bot and the worker until cancelled."""
        self.queue.reclaim_stuck()
        self.schedule_digest()
        worker = self.worker()
        tasks = [asyncio.create_task(self.bot.poll()), asyncio.create_task(worker.run())]
        try:
            await asyncio.gather(*tasks)
        finally:
            self.bot.stop()
            worker.stop()
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await self.bot.api.aclose()
            await self.ai.aclose()
            self.conn.close()


def build(settings: Settings, clock: Clock = utcnow) -> App:
    """Wire Sarthi from ``settings``. Raises ``SetupError`` when the bot token is missing."""
    if settings.telegram_bot_token is None:
        raise SetupError("SARTHI_TELEGRAM_BOT_TOKEN is not set in .env (create a bot with @BotFather)")
    conn = connect(settings.db_path)
    queue = TaskQueue(TaskStore(conn, clock=clock), clock=clock)
    jobs, matches = JobStore(conn, clock=clock), MatchStore(conn, clock=clock)
    ai = AiClient(
        resolve(settings.llm_fast_models, settings),
        AiStore(conn, clock=clock),
        daily_token_budget=settings.llm_daily_token_budget,
        requests_per_minute=settings.llm_requests_per_minute,
        clock=clock,
    )
    path = profile_path(settings.data_dir)
    matcher = Matcher(jobs, matches, queue, lambda: load_profile(path), ai=LlmMatchScorer(ai), clock=clock)
    bot = TelegramBot(
        TelegramApi(settings.telegram_bot_token),
        ChatStore(conn, clock=clock),
        jobs,
        matches,
        queue,
        clock=clock,
    )
    return App(settings, conn, queue, bot, matcher, ai, clock)
