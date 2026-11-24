"""Builds Sarthi from settings and runs it: the only module that knows every part (composition root)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from sarthi.ai.browser import next_step
from sarthi.ai.client import AiClient
from sarthi.ai.providers import resolve
from sarthi.ai.scoring import LlmMatchScorer
from sarthi.answers.handler import FormChecker
from sarthi.apply.agent import ApplicationBrowser
from sarthi.apply.engine import SITE_NAMES, ApplyEngine
from sarthi.apply.engine import login as login_to
from sarthi.apply.handler import Applier
from sarthi.apply.screen import ScreenError, login_screen
from sarthi.chat.autoapply import AutoApplySettings
from sarthi.chat.pairing import CODE_LIFETIME, OWNER_KEY, code_hash, new_code, owner_chat
from sarthi.chat.setup import SetupFlow
from sarthi.chat.telegram.api import TelegramApi
from sarthi.chat.telegram.bot import OFFSET_KEY, PAUSED_KEY, TelegramBot
from sarthi.chat.telegram.connect import (
    STEP_KEY,
    ConnectError,
    Connector,
    save_bot,
    saved_bot,
)
from sarthi.chat.telegram.link import MODE_KEY, PAIR_LINK_KEY, RelayError, RelayLink, relay_link
from sarthi.clock import Clock, from_iso, to_iso, utcnow
from sarthi.config import Settings
from sarthi.db.ai_store import AiStore
from sarthi.db.answers import AnswerStore
from sarthi.db.applications import ApplicationStore
from sarthi.db.chat import ChatStore
from sarthi.db.connection import connect
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.replies import ReplyStore
from sarthi.db.source_state import SourceStateStore
from sarthi.db.tasks import TaskRecord, TaskStore
from sarthi.documents.handler import DocumentMaker
from sarthi.matching.decide import Thresholds
from sarthi.matching.handler import LOGIC_VERSION, Matcher, match_version
from sarthi.matching.learning import RECENT as LEARN_FROM
from sarthi.matching.learning import learned_thresholds
from sarthi.profile.master import load_master, master_path
from sarthi.profile.models import Profile
from sarthi.profile.repair import repair
from sarthi.profile.resume import ResumeError, extract_text
from sarthi.profile.store import load_profile, profile_path, save_profile
from sarthi.queue import names
from sarthi.queue.tasks import PermanentError, Priority, RetryableError, TaskQueue
from sarthi.queue.worker import Handler, Worker
from sarthi.sources.aggregators import AdzunaSource, JoobleSource
from sarthi.sources.base import Source
from sarthi.sources.catalog import CatalogSource
from sarthi.sources.fill import TextFiller
from sarthi.sources.fulltext import Fetcher
from sarthi.sources.ingest import Ingestor
from sarthi.sources.schedule import SourceScheduler, plan_sources
from sarthi.tracking.handler import Mailbox, ReplyTracker
from sarthi.web.actions import Controls, Pending
from sarthi.web.server import NoFreePortError, make_server

log = logging.getLogger(__name__)

WORKER_CONCURRENCY = 3
USER_CONCURRENCY = 2
BROWSER_PROFILE = "browser-profile"  # the user's site logins (LinkedIn, Naukri), kept on their computer
BACKGROUND_TASKS = frozenset(
    {names.SOURCE_CHECK, names.FILL_TEXT, names.MATCH, names.CATCHUP, names.MAIL_CHECK, names.QUIET_CHECK}
)
MATCH_LOGIC_KEY = "matching.logic"
ALIVE_KEY = "app.alive"  # last time a running Sarthi was seen (ISO), for catch-up after the computer was off
AUTO_CATCHUP_AFTER = timedelta(hours=6)
CATCHUP_POLL_S = 2.0
REMATCH_AFTER_UPGRADE = timedelta(days=7)
SNIPPET_CHARS = 300  # a saved description this short is a title and a line: its page is read first
TICK_SECONDS = 60
PAIR_CHECK_S = 3.0
PAIR_RETRY_S = 30.0
CONNECT_CHECK_S = 3.0  # one-tap setup: how often to ask whether the user's bot was made
CONNECT_SESSION = timedelta(minutes=15)  # a setup link's life (the connector's own limit)
NOT_CONNECTED = "0:waiting-for-one-tap-setup"  # stands in until the user's bot token arrives


class SetupError(Exception):
    """Sarthi cannot start because something is not configured."""


def next_digest_at(now: datetime, hour: int) -> datetime:
    """The next time the local clock shows ``hour``:00 (today if still ahead, else tomorrow)."""
    local = now.astimezone()
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return target if target > local else target + timedelta(days=1)


class WebActions:
    """What the dashboard may change, done the way the Telegram commands and buttons do it."""

    def __init__(self, app: App) -> None:
        """Act on ``app``'s stores, queue and bot."""
        self._app = app
        self._path = profile_path(app.settings.data_dir)

    def profile(self) -> Profile | None:
        """The saved profile."""
        return load_profile(self._path)

    def save_profile(self, profile: Profile) -> int:
        """Save it and queue recent jobs to be matched again (as a Telegram setup save does)."""
        save_profile(self._path, profile)
        recent = self._app.bot.jobs.ids_seen_since(self._app.clock() - REMATCH_AFTER_UPGRADE)
        for job_id in recent:
            self._app.queue.enqueue(names.MATCH, job_id, priority=Priority.BACKGROUND)
        return len(recent)

    def resume_text(self, data: bytes) -> str:
        """The uploaded resume's text; ``ValueError`` with the reason when it cannot be read."""
        try:
            return extract_text(data)
        except ResumeError as error:
            raise ValueError(str(error)) from None

    def controls(self) -> Controls:
        """Pause and auto-apply as they stand."""
        auto = self._app.bot.auto
        return Controls(
            paused=bool(self._app.bot.chats.get_state(PAUSED_KEY)),
            auto_line=auto.line(),
            auto_wait_minutes=round(auto.settings.wait_s / 60),
            auto_daily_limit=auto.settings.daily_limit,
        )

    def set_paused(self, paused: bool) -> None:
        """/pause or /resume."""
        self._app.bot.chats.set_state(PAUSED_KEY, "1" if paused else None)

    def set_auto_line(self, line: str) -> str | None:
        """/autoapply off or /autoapply <percent>."""
        return self._app.bot.auto.change_line(line)

    def choose(self, job_id: str, action: str) -> bool:
        """Apply, skip or later, as the job's Telegram buttons do."""
        if self._app.bot.jobs.get(job_id) is None:
            return False
        self._app.bot.choose(job_id, action)
        return True

    def pending(self) -> dict[str, Pending]:
        """Filled forms waiting for Submit."""
        flow = self._app.bot.applying
        if flow is None:
            return {}
        return {job_id: Pending(job_id, shot) for job_id, (_, shot) in flow.waiting().items()}

    def submit(self, job_id: str) -> bool:
        """Send a waiting form, approved on the dashboard."""
        flow = self._app.bot.applying
        waiting = flow.waiting().get(job_id) if flow is not None else None
        if flow is None or waiting is None:
            return False
        flow.submit(job_id, waiting[0])
        return True

    def cancel(self, job_id: str) -> bool:
        """Drop a waiting form."""
        flow = self._app.bot.applying
        if flow is None or job_id not in flow.waiting():
            return False
        flow.cancel(job_id)
        return True


@dataclass
class App:
    """Everything a running Sarthi needs, wired together."""

    settings: Settings
    conn: sqlite3.Connection
    queue: TaskQueue
    bot: TelegramBot
    matcher: Matcher
    sources: SourceScheduler
    documents: DocumentMaker
    forms: FormChecker
    applier: Applier
    tracker: ReplyTracker
    filler: TextFiller
    ai_clients: tuple[AiClient, ...]
    clock: Clock
    relay: RelayLink | None = None  # the shared bot's relay, when no bot token of one's own is set
    connector: Connector | None = None  # one-tap setup still to do: no bot yet
    _login: asyncio.Task[None] | None = None

    def schedule_digest(self) -> None:
        """Queue the next daily digest (idempotent: one task per local date)."""
        at = next_digest_at(self.clock(), self.settings.digest_hour)
        delay = (at - self.clock()).total_seconds()
        self.queue.enqueue(names.DIGEST, at.date().isoformat(), delay=delay, priority=Priority.NORMAL)

    async def _digest(self, task: TaskRecord) -> None:
        await self.bot.handle_digest(task)
        self.schedule_digest()

    def handlers(self) -> dict[str, Handler]:
        """Every task type and what runs it."""
        return {
            names.SOURCE_CHECK: self.sources.handle,
            names.FILL_TEXT: self.filler.handle,
            names.MATCH: self.matcher.handle,
            names.CATCHUP: self.sources.handle_catchup,
            names.ALERT: self.bot.handle_alert,
            names.DIGEST: self._digest,
            names.CATCHUP_REPORT: self.bot.handle_catchup_report,
            names.DOCUMENTS: self.documents.handle,
            names.DOCUMENTS_READY: self.bot.handle_documents_ready,
            names.FORM_CHECK: self.forms.handle,
            names.ASK_QUESTIONS: self.bot.handle_ask_questions,
            names.APPLY: self.applier.handle_apply,
            names.AUTO_APPLY: self.bot.handle_auto_apply,
            names.SUBMIT: self.applier.handle_submit,
            names.APPLY_PREVIEW: self.bot.handle_apply_preview,
            names.APPLY_RESULT: self.bot.handle_apply_result,
            names.MAIL_CHECK: self.tracker.handle_mail,
            names.QUIET_CHECK: self.tracker.handle_quiet,
            names.REPLY_NOTICE: self.bot.handle_reply_notice,
            names.FOLLOWUP_NOTICE: self.bot.handle_followup_notice,
            names.SOURCE_NOTICE: self.bot.handle_source_notice,
        }

    def worker(self) -> Worker:
        """Background work: reading sources and matching (slow, AI-heavy, never what the user waits on)."""
        handlers = {t: h for t, h in self.handlers().items() if t in BACKGROUND_TASKS}
        return Worker(self.queue, handlers, concurrency=WORKER_CONCURRENCY, name="background")

    def user_worker(self) -> Worker:
        """What the user asked for or waits on (alerts, documents, applying): never behind matching."""
        handlers = {t: h for t, h in self.handlers().items() if t not in BACKGROUND_TASKS}
        return Worker(self.queue, handlers, concurrency=USER_CONCURRENCY, name="user")

    async def _tick_sources(self, worker: Worker) -> None:
        while True:
            self.bot.chats.set_state(ALIVE_KEY, to_iso(self.clock()))
            await self.applier.engine.close_idle()  # forms nobody approved in time
            if self.sources.tick():
                worker.wake()
            await asyncio.sleep(TICK_SECONDS)

    async def catch_up_after_absence(self) -> bool:
        """When Sarthi was off for a while, start one catch-up over the gap (capped at 7 days)."""
        last = self.bot.chats.get_state(ALIVE_KEY)
        chat_id = owner_chat(self.bot.chats)
        if not last or chat_id is None or self.sources.profile() is None:
            return False
        try:
            gap = self.clock() - from_iso(last)
        except ValueError:
            return False
        if gap < AUTO_CATCHUP_AFTER:
            return False
        return await self.bot.catchup.start_after_absence(chat_id, math.ceil(gap.total_seconds() / 3600))

    def rematch_after_upgrade(self) -> int:
        """Redo recent matches once when the matching logic changed. Returns how many were queued."""
        chats, jobs = self.bot.chats, self.bot.jobs
        if chats.get_state(MATCH_LOGIC_KEY) == LOGIC_VERSION:
            return 0
        ids = jobs.ids_active_since(self.clock() - REMATCH_AFTER_UPGRADE)
        for job_id in ids:
            self.queue.enqueue(self._rematch_task(job_id), job_id, priority=Priority.NORMAL)
        chats.set_state(MATCH_LOGIC_KEY, LOGIC_VERSION)
        log.info("matching logic changed: %d recent job(s) queued to be matched again", len(ids))
        return len(ids)

    def _rematch_task(self, job_id: str) -> str:
        """``fill_text`` for a job saved before its page was read (a title or a snippet only), else ``match``.

        Matching it again on a title alone checks nothing the new logic reads (years asked, level, skills).
        """
        if not self.settings.fill_text:
            return names.MATCH
        job = self.bot.jobs.get(job_id)
        text = self.bot.jobs.full_text(job_id) or (job.description if job else "")
        if job is None or len(text) >= SNIPPET_CHARS:  # (old saves mark even a title-only job "complete")
            return names.MATCH
        return names.FILL_TEXT

    async def run(self) -> None:
        """Run the bot, the source scheduler and the worker until cancelled."""
        self.queue.reclaim_stuck()
        self.rematch_after_upgrade()
        self.schedule_digest()
        self.tracker.schedule()
        self.tracker.schedule_quiet()
        try:
            await self.catch_up_after_absence()
        except (RetryableError, PermanentError) as error:
            log.warning("could not start the catch-up after a break: %s", error)
        worker, user = self.worker(), self.user_worker()
        self.bot.site_login = self.site_login
        tasks = [
            asyncio.create_task(self._telegram()),
            asyncio.create_task(worker.run()),
            asyncio.create_task(user.run()),
            asyncio.create_task(self._tick_sources(worker)),
        ]
        dashboard = self._dashboard()
        if dashboard is not None:
            tasks.append(asyncio.create_task(dashboard.serve()))
        try:
            await asyncio.gather(*tasks)
        finally:
            if dashboard is not None:
                dashboard.should_exit = True
            await self._shutdown((worker, user), tasks)

    async def site_login(self, chat_id: int, site: str) -> None:
        """/login from Telegram: open the site's login page, on a screen in the browser when in Docker."""
        if self._login is not None and not self._login.done():
            await self.bot.api.send_message(
                chat_id, "A login is already open: finish or close that one first."
            )
            return
        self._login = asyncio.create_task(self._log_in(chat_id, site))

    async def _log_in(self, chat_id: int, site: str) -> None:
        name = SITE_NAMES.get(site, site)
        send = self.bot.api.send_message
        # The saved logins are one browser profile, which one browser at a time may open: the apply browser
        # closes (forms waiting for Submit are filled again when tapped) and reopens with the new login.
        await self.applier.engine.aclose()
        try:
            async with login_screen(self.settings.screen_port, self.settings.dashboard_host) as screen:
                if screen.url is None:
                    await send(chat_id, f"A browser window opened on your computer. Log in to {name} there.")
                else:
                    await send(
                        chat_id,
                        f"Open this on the computer running Sarthi and log in to {name} there "
                        f"(the link works for 10 minutes):\n{screen.url}",
                    )
                done = await login_to(site, self.settings.data_dir / BROWSER_PROFILE, env=screen.env)
        except ScreenError as error:
            await send(chat_id, f"Could not open a browser for the login: {error}")
            return
        except Exception:  # a browser that fails to start must not stop Sarthi
            log.exception("login to %s failed", site)
            await send(chat_id, f"The {name} login could not be opened. Try /login again.")
            return
        await send(
            chat_id,
            f"✅ Logged in to {name}: Sarthi can apply there now."
            if done
            else f"Not logged in to {name} (the window was closed or 10 minutes passed). Try /login again.",
        )

    async def _telegram(self) -> None:
        """Make the user's bot first when one-tap setup is still to do; then serve the chat."""
        if self.connector is not None:
            await self.connect_bot(self.connector)
        await asyncio.gather(self.bot.poll(), self.offer_pairing())

    async def connect_bot(self, connector: Connector) -> None:
        """One-tap setup: show the connector's link until the user's new bot arrives, then switch to it."""
        chats = self.bot.chats
        while True:
            try:
                session = await connector.session()
            except ConnectError as error:
                log.warning("one-tap Telegram setup is not reachable: %s", error)
                await asyncio.sleep(PAIR_RETRY_S)
                continue
            chats.set_state(PAIR_LINK_KEY, session.link)
            chats.set_state(STEP_KEY, "create")
            log.info(
                "Connect Telegram: open %s and tap Create (or use the button at http://127.0.0.1:%d)",
                session.link,
                self.settings.dashboard_port,
            )
            renew_at = self.clock() + CONNECT_SESSION - timedelta(minutes=1)
            made = None
            while made is None and self.clock() < renew_at:
                await asyncio.sleep(CONNECT_CHECK_S)
                try:
                    made = await connector.collect(session)
                except ConnectError as error:
                    log.warning("one-tap Telegram setup: %s", error)
            if made is not None:
                break
        save_bot(self.settings.data_dir, made)
        self.bot.api.use_token(made.token)
        for key in (OFFSET_KEY, OWNER_KEY, PAIR_LINK_KEY):  # a new bot: nothing carries over
            chats.set_state(key, None)
        chats.set_state(MODE_KEY, f"managed:{made.username}")
        chats.set_state(STEP_KEY, "start2")
        self.connector = None
        await connector.aclose()
        log.info("Your Telegram bot @%s is ready", made.username)

    async def offer_pairing(self) -> None:
        """Keep a fresh link to connect Telegram on show (log and dashboard) until one is used."""
        if self.relay is None:
            await self._offer_own_link()
            return
        chats = self.bot.chats
        while owner_chat(chats) is None:
            code = new_code(chats, self.clock())
            try:
                await self.bot.api.set_pairing_code(code_hash(code))
            except (RetryableError, PermanentError) as error:
                log.warning("could not reach the shared Sarthi bot to make a link: %s", error)
                await asyncio.sleep(PAIR_RETRY_S)
                continue
            link = self.relay.pairing_link(code)
            chats.set_state(PAIR_LINK_KEY, link)
            log.info(
                "Connect Telegram: open %s on your phone (or at http://127.0.0.1:%d)",
                link,
                self.settings.dashboard_port,
            )
            renew_at = self.clock() + CODE_LIFETIME - timedelta(minutes=1)
            while owner_chat(chats) is None and self.clock() < renew_at:
                await asyncio.sleep(PAIR_CHECK_S)
        chats.set_state(PAIR_LINK_KEY, None)

    async def _offer_own_link(self) -> None:
        """A bot of the user's own (one-tap or @BotFather): a t.me link with a one-time code, until used."""
        chats = self.bot.chats
        username = ""
        while owner_chat(chats) is None and not username:
            try:
                username = await self.bot.api.username()
            except (RetryableError, PermanentError) as error:
                log.warning("could not ask Telegram for the bot's name: %s", error)
                await asyncio.sleep(PAIR_RETRY_S)
        while owner_chat(chats) is None:
            code = new_code(chats, self.clock())
            link = f"https://t.me/{username}?start={code}"
            chats.set_state(PAIR_LINK_KEY, link)
            log.info(
                "Connect Telegram: open %s and tap Start (or see http://127.0.0.1:%d)",
                link,
                self.settings.dashboard_port,
            )
            renew_at = self.clock() + CODE_LIFETIME - timedelta(minutes=1)
            while owner_chat(chats) is None and self.clock() < renew_at:
                await asyncio.sleep(PAIR_CHECK_S)
        chats.set_state(PAIR_LINK_KEY, None)
        chats.set_state(STEP_KEY, None)

    def _dashboard(self) -> Any:
        """The local dashboard's server, or None when it is off or no port is free (Sarthi runs on)."""
        if not self.settings.dashboard:
            return None
        try:
            return make_server(
                self.settings.db_path,
                self.settings.dashboard_port,
                host=self.settings.dashboard_host,
                own_signals=False,
                actions=WebActions(self),
                data_dir=self.settings.data_dir,
            )
        except NoFreePortError as error:
            log.warning("dashboard not started: %s", error)
            return None

    def catchup_problem(self) -> str | None:
        """Why a catch-up cannot run now (not paired, no profile), or None."""
        if owner_chat(self.bot.chats) is None:
            return "Telegram is not paired yet: run `uv run sarthi pair` first"
        if self.sources.profile() is None:
            return "no profile yet: send /setup to your Sarthi bot first"
        return None

    async def run_catchup(self, hours: int, timeout: timedelta = timedelta(hours=1)) -> bool:
        """One catch-up, then exit (for a computer that is on only part of the day, e.g. from cron).

        Returns False when the results could not be sent within ``timeout``.
        """
        self.queue.reclaim_stuck()
        self.rematch_after_upgrade()
        if not self.bot.catchup.start(hours):
            log.info("a catch-up is already running; waiting for it")
        worker, user = self.worker(), self.user_worker()
        tasks = [asyncio.create_task(worker.run()), asyncio.create_task(user.run())]
        deadline = self.clock() + timeout
        try:
            while self.clock() < deadline:
                await asyncio.sleep(CATCHUP_POLL_S)
                if not self.bot.catchup.running():
                    return True
            return False
        finally:
            await self._shutdown((worker, user), tasks)

    async def _shutdown(self, workers: tuple[Worker, ...], tasks: list[asyncio.Task[None]]) -> None:
        self.bot.stop()
        for worker in workers:
            worker.stop()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.bot.inbox is not None:
            await self.bot.inbox.close()
        await self.bot.api.aclose()
        await self.applier.engine.aclose()
        await self.filler.aclose()
        for client in self.ai_clients:
            await client.aclose()
        self.conn.close()


def _ai(
    settings: Settings,
    models: list[str],
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    background: bool = False,
) -> AiClient:
    """A client for ``models``; a ``background`` one uses no local model and gives way to the user's calls."""
    endpoints = resolve(models, settings)
    if background:
        endpoints = [endpoint for endpoint in endpoints if not endpoint.free]
    return AiClient(
        endpoints,
        AiStore(conn, clock=clock),
        daily_token_budget=settings.llm_daily_token_budget,
        requests_per_minute=settings.llm_requests_per_minute,
        clock=clock,
        background=background,
    )


def _browser_ai(client: AiClient) -> Any:
    """The application browser's AI step on ``client`` (the quality models), or None without any model."""
    if not client.available:
        return None

    async def decide(page_text: str, fields: str, buttons: str) -> tuple[str, str, str] | None:
        step = await next_step(client, page_text, fields, buttons)
        return (step.state, step.click, step.why) if step is not None else None

    return decide


def aggregator_sources(settings: Settings) -> list[Source]:
    """Role searches beyond the job boards: the company-board snapshot, and Adzuna and Jooble when keyed."""
    found: list[Source] = [CatalogSource()] if settings.catalog else []
    if settings.adzuna_app_id and settings.adzuna_app_key is not None:
        found.append(AdzunaSource(settings.adzuna_app_id, settings.adzuna_app_key))
    if settings.jooble_api_key is not None:
        found.append(JoobleSource(settings.jooble_api_key))
    return found


def repair_saved_profile(path: Path) -> None:
    """Remove junk that older versions saved into the profile (e.g. "Use skills" as a skill)."""
    saved = load_profile(path)
    if saved is None:
        return
    fixed = repair(saved)
    if fixed is not saved:
        save_profile(path, fixed)
        log.info("cleaned leftover instructions from the saved profile's roles and skills")


def _telegram_api(settings: Settings) -> tuple[TelegramApi, str, RelayLink | None, Connector | None]:
    """The bot Sarthi talks through, chosen in a fixed order.

    A token of the user's own (@BotFather); the bot one-tap setup made earlier; the shared bot's relay; or
    one-tap setup still to do (the token arrives while Sarthi runs).
    """
    relay: RelayLink | None = None
    connector: Connector | None = None
    made = saved_bot(settings.data_dir)
    if settings.telegram_bot_token is not None:
        api, mode = TelegramApi(settings.telegram_bot_token), "own"
    elif made is not None:  # made earlier by one-tap setup
        api, mode = TelegramApi(made.token), f"managed:{made.username}"
    elif settings.relay_url:
        try:
            relay = relay_link(settings.data_dir, settings.relay_url)
        except RelayError as error:
            raise SetupError(f"the shared Sarthi bot ({settings.relay_url}): {error}") from None
        api, mode = TelegramApi(relay.secret, base_url=relay.url), f"relay:{relay.url}"
    elif settings.telegram_connect_url:
        try:
            connector = Connector(settings.telegram_connect_url)
        except ConnectError as error:
            raise SetupError(f"one-tap Telegram setup ({settings.telegram_connect_url}): {error}") from None
        api, mode = TelegramApi(SecretStr(NOT_CONNECTED)), "connect"  # a real token arrives during setup
    else:
        raise SetupError(
            "no Telegram bot: set SARTHI_TELEGRAM_CONNECT_URL (one-tap setup), or SARTHI_TELEGRAM_BOT_TOKEN"
            " (a bot of your own from @BotFather)"
        )
    return api, mode, relay, connector


def build(settings: Settings, clock: Clock = utcnow) -> App:
    """Wire Sarthi from ``settings``. Raises ``SetupError`` when the bot token is missing."""
    api, mode, relay, connector = _telegram_api(settings)
    conn = connect(settings.db_path)
    queue = TaskQueue(TaskStore(conn, clock=clock), clock=clock)
    jobs, matches = JobStore(conn, clock=clock), MatchStore(conn, clock=clock)
    chats, states = ChatStore(conn, clock=clock), SourceStateStore(conn, clock=clock)
    fast, quality = (
        _ai(settings, settings.llm_fast_models, conn, clock),
        _ai(settings, settings.llm_quality_models, conn, clock),
    )
    # Matching scores every new job, thousands a day: a local model doing that keeps a laptop CPU at full
    # load, and on a free cloud tier it would use up the limits the user's documents need. It gets only the
    # cloud fast models, behind the user's calls; with none answering, jobs get the free score instead.
    matching_ai = _ai(settings, settings.llm_fast_models, conn, clock, background=True)
    path = profile_path(settings.data_dir)
    repair_saved_profile(path)

    def profile() -> Profile | None:
        return load_profile(path)

    previous = chats.get_state(MODE_KEY)
    if previous != mode:
        # Another bot (own bot <-> shared bot): its update numbers and chat link do not carry over. An
        # install from before modes were recorded used its own bot: nothing to reset for it.
        for key in () if previous is None and mode == "own" else (OFFSET_KEY, OWNER_KEY, PAIR_LINK_KEY):
            chats.set_state(key, None)
        chats.set_state(MODE_KEY, mode)
    setup = SetupFlow(api, chats, quality, path, jobs=jobs, queue=queue, clock=clock)

    def profile_version() -> str | None:
        current = profile()
        return match_version(current) if current else None

    bot = TelegramBot(
        api,
        chats,
        jobs,
        matches,
        queue,
        setup=setup,
        group_messages=True,
        profile_version=profile_version,
        documents=DocumentStore(conn, clock=clock),
        answers=AnswerStore(conn, clock=clock),
        applications=ApplicationStore(conn, clock=clock),
        data_dir=settings.data_dir,
        auto_apply=AutoApplySettings(
            line=settings.auto_apply_min_score,
            daily_limit=settings.auto_apply_daily_limit,
            wait_s=settings.auto_apply_wait_minutes * 60,
        ),
        clock=clock,
    )

    def chat_busy() -> bool:
        return bot.inbox is not None and bot.inbox.busy

    def learned() -> Thresholds:
        return learned_thresholds(Thresholds(), matches.recent_choices(LEARN_FROM)).thresholds

    matcher = Matcher(
        jobs,
        matches,
        queue,
        profile,
        ai=LlmMatchScorer(matching_ai),
        ai_paused=chat_busy,
        learned=learned,
        clock=clock,
    )
    planned = plan_sources(
        settings.job_boards,
        settings.watch_companies,
        timedelta(minutes=settings.boards_every_minutes),
        timedelta(minutes=settings.companies_every_minutes),
        feeds=settings.feeds,
        feeds_every=timedelta(minutes=settings.feeds_every_minutes),
        freehire=settings.freehire,
        searches=aggregator_sources(settings),
    )
    ingestor = Ingestor(jobs, queue, states, fill_text=settings.fill_text)
    sources = SourceScheduler(planned, ingestor, states, queue, profile, clock=clock)
    filler = TextFiller(jobs, queue, Fetcher(render_js=settings.fill_render_js))
    documents = DocumentMaker(
        jobs,
        DocumentStore(conn, clock=clock),
        queue,
        profile,
        ai=quality,
        data_dir=settings.data_dir,
        clock=clock,
    )
    forms = FormChecker(
        jobs,
        DocumentStore(conn, clock=clock),
        AnswerStore(conn, clock=clock),
        queue,
        profile,
        data_dir=settings.data_dir,
        chats=chats,
    )
    applier = Applier(
        ApplyEngine(
            settings.data_dir / "screenshots",
            profile_dir=settings.data_dir / BROWSER_PROFILE,
            headless=settings.apply_headless,
            browser=ApplicationBrowser(_browser_ai(quality)),
        ),
        jobs,
        DocumentStore(conn, clock=clock),
        AnswerStore(conn, clock=clock),
        ApplicationStore(conn, clock=clock),
        queue=queue,
        load_profile=profile,
        data_dir=settings.data_dir,
        daily_limit=settings.apply_daily_limit,
        chats=chats,
        clock=clock,
    )
    mailbox = (
        Mailbox(
            settings.mail_imap_host,
            settings.mail_imap_port,
            settings.mail_user,
            settings.mail_password.get_secret_value(),
            settings.mail_folder,
        )
        if settings.mail_enabled and settings.mail_password is not None
        else None
    )

    def own_name() -> str:
        master = load_master(master_path(settings.data_dir))
        return master.name if master else ""

    tracker = ReplyTracker(
        ReplyStore(conn, clock=clock),
        ApplicationStore(conn, clock=clock),
        jobs,
        chats,
        queue,
        ai=fast,
        mailbox=mailbox,
        every=timedelta(minutes=settings.mail_every_minutes),
        followup_after=timedelta(days=settings.followup_after_days),
        name=own_name,
        clock=clock,
    )
    return App(
        settings,
        conn,
        queue,
        bot,
        matcher,
        sources,
        documents,
        forms,
        applier,
        tracker,
        filler,
        (fast, quality, matching_ai),
        clock,
        relay,
        connector,
    )
