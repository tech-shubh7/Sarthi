"""Catch-up mode in the chat (docs/ARCHITECTURE.md §2.18): "show me what I missed in the last N days".

``/catchup`` offers 24 hours, 2 days, 5 days or 7 days. A ``catchup`` task (run by the source scheduler)
searches every source over that window and queues new jobs for matching; then a ``catchup_report`` task waits
until matching has finished and sends one batch: a summary, the best matches as cards with buttons, and every
match as a spreadsheet file. While a catch-up runs, single alerts are held so the results arrive together.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from html import escape

from sarthi.chat.export import Row, matches_csv
from sarthi.chat.telegram.api import Markup, TelegramApi, buttons
from sarthi.clock import Clock, from_iso, to_iso, utcnow
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue import names
from sarthi.queue.tasks import DeferError, Priority, TaskQueue

log = logging.getLogger(__name__)

ACTIVE_KEY = "catchup.active"  # when the running catch-up started (ISO time)
WINDOWS: dict[int, str] = {24: "24 hours", 48: "2 days", 120: "5 days", 168: "7 days"}
CARDS = 10
WAIT_FOR_MATCHING_S = 20
MAX_WAIT = timedelta(minutes=45)  # a slow local AI must not hold the results forever
STALE_AFTER = timedelta(hours=2)  # a catch-up that crashed stops holding alerts
_DONE_STATUSES = frozenset({JobStatus.SKIPPED, JobStatus.APPLY, JobStatus.APPLIED})

ASK = "🔎 <b>Catch-up</b>: I'll search every job source for what was posted in the window you pick."

SendJob = Callable[[int, str, JobStatus], Awaitable[bool]]


MAX_HOURS = 168


def window_label(hours: int) -> str:
    """A window for people: "2 days", "24 hours", "36 hours"."""
    if hours in WINDOWS:
        return WINDOWS[hours]
    days, rest = divmod(hours, 24)
    return f"{days} day{'s' if days != 1 else ''}" if days and not rest else f"{hours} hours"


def window_buttons() -> Markup:
    """One button per catch-up window."""
    labels = list(WINDOWS.items())
    return buttons(
        [(f"Last {label}", f"catchup:{hours}") for hours, label in labels[:2]],
        [(f"Last {label}", f"catchup:{hours}") for hours, label in labels[2:]],
    )


def parse_window(data: str) -> int | None:
    """Hours from a ``catchup:<hours>`` button, or None when it is not one Sarthi offers."""
    _, _, value = data.partition(":")
    return int(value) if value.isdigit() and int(value) in WINDOWS else None


class CatchUp:
    """Starts catch-up runs from the chat and sends their results."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        api: TelegramApi,
        chats: ChatStore,
        jobs: JobStore,
        matches: MatchStore,
        queue: TaskQueue,
        *,
        send_job: SendJob,
        clock: Clock = utcnow,
    ) -> None:
        """Send with ``api``; ``send_job`` sends one job card with its buttons."""
        self.api = api
        self.chats = chats
        self.jobs = jobs
        self.matches = matches
        self.queue = queue
        self._send_job = send_job
        self._clock = clock

    def running(self) -> bool:
        """True while a catch-up is collecting results (single alerts wait for the batch)."""
        started = self.chats.get_state(ACTIVE_KEY)
        if not started:
            return False
        try:
            return self._clock() - from_iso(started) < STALE_AFTER
        except ValueError:
            return False

    def start(self, hours: int) -> bool:
        """Queue a catch-up over the last ``hours`` (1-168). False when one is already running."""
        hours = max(1, min(MAX_HOURS, hours))
        if self.running():
            return False
        now = self._clock()
        self.chats.set_state(ACTIVE_KEY, to_iso(now))
        self.queue.enqueue(
            names.CATCHUP, str(hours), {"hours": hours, "started": to_iso(now)}, priority=Priority.ALERT
        )
        return True

    async def on_button(self, chat_id: int, data: str) -> None:
        """A window button was tapped."""
        hours = parse_window(data)
        if hours is None:
            return
        if not self.start(hours):
            await self.api.send_message(chat_id, "A catch-up is already running; its results are coming.")
            return
        await self.api.send_message(
            chat_id,
            f"🔎 Searching every source for jobs from the last {window_label(hours)}. "
            "I'll send the results together in a few minutes.",
        )

    async def start_after_absence(self, chat_id: int, hours: int) -> bool:
        """Sarthi was off for ``hours``: collect what was missed into one batch instead of many alerts."""
        if not self.start(hours):
            return False
        await self.api.send_message(
            chat_id,
            f"👋 Welcome back. Sarthi was off for about {window_label(min(hours, MAX_HOURS))}; "
            "I'm checking every source for what you missed and will send it together.",
        )
        return True

    def _rows(self, since: datetime) -> list[Row]:
        rows: list[Row] = []
        for job_id in self.jobs.ids_active_since(since):
            match = self.matches.get(job_id)
            if match is None or match.decision not in ("alert", "digest"):
                continue
            if self.chats.job_status(job_id) in _DONE_STATUSES:
                continue
            job = self.jobs.get(job_id)
            if job is not None:
                rows.append(Row(job, match))
        rows.sort(key=lambda row: (row.match.score, row.match.decision == "alert"), reverse=True)
        return rows

    async def handle_report(self, task: TaskRecord, chat_id: int) -> None:
        """``catchup_report`` task: when matching is done, send the batch and stop holding alerts."""
        started = from_iso(str(task.payload.get("started") or to_iso(self._clock())))
        waiting = self.queue.active(names.MATCH)
        if waiting and self._clock() - started < MAX_WAIT:
            raise DeferError(WAIT_FOR_MATCHING_S)
        hours = int(task.payload.get("hours") or 24)
        rows = self._rows(started - timedelta(hours=hours))
        try:
            await self._send(chat_id, hours, task, rows, unfinished=waiting)
        finally:
            self.chats.set_state(ACTIVE_KEY, None)

    async def _send(
        self, chat_id: int, hours: int, task: TaskRecord, rows: list[Row], unfinished: int
    ) -> None:
        label = window_label(hours)
        strong = sum(1 for row in rows if row.match.decision == "alert")
        lines = [f"<b>Catch-up: last {label}</b>"]
        sources, failed = int(task.payload.get("sources") or 0), list(task.payload.get("failed") or [])
        if sources:
            lines.append(f"Searched {sources} source(s), {int(task.payload.get('new') or 0)} new job(s).")
        if failed:
            lines.append(f"⚠️ Could not read: {escape(', '.join(str(f) for f in failed[:5]))}")
        if unfinished:
            lines.append(f"⏳ {unfinished} job(s) were still being checked; they'll come as normal alerts.")
        if not rows:
            lines.append("No jobs in that window match your profile.")
            await self.api.send_message(chat_id, "\n".join(lines))
            return
        lines.append(
            f"<b>{len(rows)}</b> match your profile ({strong} strong). Best {min(CARDS, len(rows))}:"
        )
        await self.api.send_message(chat_id, "\n".join(lines))
        for row in rows[:CARDS]:
            await self._send_job(chat_id, row.match.job_id, JobStatus.DIGESTED)
        name = f"sarthi-matches-last-{label.replace(' ', '-')}-{self._clock().date().isoformat()}.csv"
        await self.api.send_document(
            chat_id, name, matches_csv(rows), caption=f"All {len(rows)} matches as a spreadsheet."
        )
