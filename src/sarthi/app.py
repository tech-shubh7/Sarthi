"""Builds Sarthi from settings and runs it: the only module that knows every part (composition root)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sarthi.ai.client import AiClient
from sarthi.ai.providers import resolve
from sarthi.ai.scoring import LlmMatchScorer
from sarthi.chat.setup import SetupFlow
from sarthi.chat.telegram.api import TelegramApi
from sarthi.chat.telegram.bot import TelegramBot
from sarthi.clock import Clock, utcnow
from sarthi.config import Settings
from sarthi.db.ai_store import AiStore
from sarthi.db.chat import ChatStore
from sarthi.db.connection import connect
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.source_state import SourceStateStore
from sarthi.db.tasks import TaskRecord, TaskStore
from sarthi.matching.handler import Matcher
from sarthi.profile.models import Profile
from sarthi.profile.repair import repair
from sarthi.profile.store import load_profile, profile_path, save_profile
from sarthi.queue import names
from sarthi.queue.tasks import Priority, TaskQueue
from sarthi.queue.worker import Worker
from sarthi.sources.ingest import Ingestor
from sarthi.sources.schedule import SourceScheduler, plan_sources

log = logging.getLogger(__name__)

WORKER_CONCURRENCY = 3
TICK_SECONDS = 60


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
    sources: SourceScheduler
    ai_clients: tuple[AiClient, ...]
    clock: Clock

    def schedule_digest(self) -> None:
        """Queue the next daily digest (idempotent: one task per local date)."""
        at = next_digest_at(self.clock(), self.settings.digest_hour)
        delay = (at - self.clock()).total_seconds()
        self.queue.enqueue(names.DIGEST, at.date().isoformat(), delay=delay, priority=Priority.NORMAL)

    async def _digest(self, task: TaskRecord) -> None:
        await self.bot.handle_digest(task)
        self.schedule_digest()

    def worker(self) -> Worker:
        """A worker for source checks, matching, alerts and digests."""
        handlers = {
            names.SOURCE_CHECK: self.sources.handle,
            names.MATCH: self.matcher.handle,
            names.ALERT: self.bot.handle_alert,
            names.DIGEST: self._digest,
        }
        return Worker(self.queue, handlers, concurrency=WORKER_CONCURRENCY, name="sarthi")

    async def _tick_sources(self, worker: Worker) -> None:
        while True:
            if self.sources.tick():
                worker.wake()
            await asyncio.sleep(TICK_SECONDS)

    async def run(self) -> None:
        """Run the bot, the source scheduler and the worker until cancelled."""
        self.queue.reclaim_stuck()
        self.schedule_digest()
        worker = self.worker()
        tasks = [
            asyncio.create_task(self.bot.poll()),
            asyncio.create_task(worker.run()),
            asyncio.create_task(self._tick_sources(worker)),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            self.bot.stop()
            worker.stop()
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if self.bot.inbox is not None:
                await self.bot.inbox.close()
            await self.bot.api.aclose()
            for client in self.ai_clients:
                await client.aclose()
            self.conn.close()


def _ai(settings: Settings, models: list[str], conn: sqlite3.Connection, clock: Clock) -> AiClient:
    return AiClient(
        resolve(models, settings),
        AiStore(conn, clock=clock),
        daily_token_budget=settings.llm_daily_token_budget,
        requests_per_minute=settings.llm_requests_per_minute,
        clock=clock,
    )


def repair_saved_profile(path: Path) -> None:
    """Remove junk that older versions saved into the profile (e.g. "Use skills" as a skill)."""
    saved = load_profile(path)
    if saved is None:
        return
    fixed = repair(saved)
    if fixed is not saved:
        save_profile(path, fixed)
        log.info("cleaned leftover instructions from the saved profile's roles and skills")


def build(settings: Settings, clock: Clock = utcnow) -> App:
    """Wire Sarthi from ``settings``. Raises ``SetupError`` when the bot token is missing."""
    if settings.telegram_bot_token is None:
        raise SetupError("SARTHI_TELEGRAM_BOT_TOKEN is not set in .env (create a bot with @BotFather)")
    conn = connect(settings.db_path)
    queue = TaskQueue(TaskStore(conn, clock=clock), clock=clock)
    jobs, matches = JobStore(conn, clock=clock), MatchStore(conn, clock=clock)
    chats, states = ChatStore(conn, clock=clock), SourceStateStore(conn, clock=clock)
    fast, quality = (
        _ai(settings, settings.llm_fast_models, conn, clock),
        _ai(settings, settings.llm_quality_models, conn, clock),
    )
    path = profile_path(settings.data_dir)
    repair_saved_profile(path)

    def profile() -> Profile | None:
        return load_profile(path)

    api = TelegramApi(settings.telegram_bot_token)
    setup = SetupFlow(api, chats, quality, path, jobs=jobs, queue=queue, clock=clock)
    bot = TelegramBot(api, chats, jobs, matches, queue, setup=setup, group_messages=True, clock=clock)

    def chat_busy() -> bool:
        return bot.inbox is not None and bot.inbox.busy

    matcher = Matcher(
        jobs, matches, queue, profile, ai=LlmMatchScorer(fast), ai_paused=chat_busy, clock=clock
    )
    planned = plan_sources(
        settings.job_boards,
        settings.watch_companies,
        timedelta(minutes=settings.boards_every_minutes),
        timedelta(minutes=settings.companies_every_minutes),
    )
    sources = SourceScheduler(planned, Ingestor(jobs, queue, states), states, queue, profile, clock=clock)
    return App(settings, conn, queue, bot, matcher, sources, (fast, quality), clock)
