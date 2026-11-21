"""Reply tracking: find emails about the user's applications and tell the user; nudge quiet applications.

Each email is read once (by Message-ID). Rules sort most emails (``triage.py``); the AI reads only interviews,
offers, tests and unclear ones. An email counts only when it matches an application Sarthi knows
(``matcher.py``), so a newsletter never becomes "an interview". Quiet applications get at most two
follow-up drafts, the first ``followup_after_days`` after applying, and none after 60 days or once the
employer has written.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sarthi.ai.client import AiClient
from sarthi.ai.replies import read_reply
from sarthi.clock import Clock, from_iso, utcnow
from sarthi.db.applications import ApplicationStore
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.db.replies import ReplyRecord, ReplyStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue import names
from sarthi.queue.tasks import Priority, RetryableError, TaskQueue
from sarthi.tracking.followup import followup_draft
from sarthi.tracking.mail import Mail, MailError, fetch_new
from sarthi.tracking.matcher import Applied, match
from sarthi.tracking.triage import Kind, triage

log = logging.getLogger(__name__)

UID_KEY = "mail.last_uid"
FIRST_LOOK_BACK = timedelta(days=30)
MIN_AI_CONFIDENCE = 0.5
MAX_REMINDERS = 2
QUIET_AFTER = timedelta(days=60)  # after this a nudge reads oddly
QUIET_EVERY = timedelta(hours=12)


@dataclass(frozen=True, slots=True)
class Mailbox:
    """Where to read mail (IMAP)."""

    host: str
    port: int
    user: str
    password: str
    folder: str = "INBOX"


class ReplyTracker:
    """Handles ``mail_check`` and ``quiet_check`` tasks."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        replies: ReplyStore,
        applications: ApplicationStore,
        jobs: JobStore,
        chats: ChatStore,
        queue: TaskQueue,
        *,
        ai: AiClient,
        mailbox: Mailbox | None,
        every: timedelta = timedelta(minutes=10),
        followup_after: timedelta = timedelta(days=10),
        name: Callable[[], str] = lambda: "",
        fetch: Callable[..., list[Mail]] = fetch_new,
        clock: Clock = utcnow,
    ) -> None:
        """Read ``mailbox`` (None: tracking is off) every ``every``; ``name`` signs follow-up drafts."""
        self.replies = replies
        self.applications = applications
        self.jobs = jobs
        self.chats = chats
        self.queue = queue
        self.ai = ai
        self.mailbox = mailbox
        self.every = every
        self.followup_after = followup_after
        self._name = name
        self._fetch = fetch
        self._clock = clock

    # ---- scheduling (two alternating keys: a task cannot re-queue itself while it runs) ----------------

    def schedule(self, delay: float = 0, key: str = "a") -> None:
        """Queue the next mailbox read (only when a mailbox is set up)."""
        if self.mailbox is not None:
            self.queue.enqueue(names.MAIL_CHECK, key, delay=delay, priority=Priority.NORMAL)

    def schedule_quiet(self, delay: float = 0, key: str = "a") -> None:
        """Queue the next look for quiet applications."""
        self.queue.enqueue(names.QUIET_CHECK, key, delay=delay, priority=Priority.BACKGROUND)

    @staticmethod
    def _other(key: str) -> str:
        return "b" if key == "a" else "a"

    # ---- reading replies ------------------------------------------------------------------------------

    def _applied(self) -> list[Applied]:
        found: list[Applied] = []
        for record in self.applications.sent():
            job = self.jobs.get(record.job_id)
            if job is None:
                continue
            when = from_iso(record.submitted_at) if record.submitted_at else None
            found.append(Applied(record.job_id, job.company, job.title, str(job.apply_url or job.url), when))
        return found

    async def handle_mail(self, task: TaskRecord) -> None:
        """``mail_check`` task: read new emails once, then queue the next read."""
        try:
            await self.check_mail()
        finally:
            self.schedule(self.every.total_seconds(), self._other(task.key))

    async def check_mail(self) -> int:
        """Read new emails; returns how many were replies about applications."""
        box = self.mailbox
        if box is None:
            return 0
        after = int(self.chats.get_state(UID_KEY) or 0)
        since = (self._clock() - FIRST_LOOK_BACK).date()
        try:
            mails = await asyncio.to_thread(
                self._fetch, box.host, box.port, box.user, box.password, after, since=since, folder=box.folder
            )
        except MailError as error:
            raise RetryableError(f"mailbox: {error}") from None
        applied = self._applied()
        found = 0
        for mail in mails:
            if not self.replies.seen(mail.message_id) and await self._read(mail, applied):
                found += 1
            self.chats.set_state(
                UID_KEY, str(max(after, mail.uid))
            )  # progress kept even if a later one fails
            after = max(after, mail.uid)
        return found

    async def _read(self, mail: Mail, applied: list[Applied]) -> bool:
        verdict = triage(mail.sender, mail.subject, mail.body)
        if verdict.kind is Kind.NOISE:
            return False
        matched = match(mail.sender, mail.subject, mail.body, sent=mail.sent_at, applications=applied)
        if matched is None:
            return False  # not about an application Sarthi knows: left alone (and not sent to any AI)
        kind: Kind | None = verdict.kind
        summary, details = "", {"signals": list(matched.signals)}
        if verdict.needs_ai:
            answer = await read_reply(self.ai, mail.sender, mail.subject, mail.body)
            if answer is None:
                raise RetryableError("the AI could not read a reply right now")
            if answer.kind == "noise" or answer.confidence < MIN_AI_CONFIDENCE:
                kind = Kind.OTHER if answer.kind != "noise" else Kind.NOISE
            else:
                kind = Kind(answer.kind)
            summary = answer.summary
            details |= {"dates": answer.dates, "actions": answer.actions}
        if kind is None or kind is Kind.NOISE:
            return False
        stored = self.replies.save(
            ReplyRecord(
                mail.message_id,
                matched.job_id,
                kind.value,
                mail.sender,
                mail.subject,
                summary,
                details,
                mail.sent_at,
            )
        )
        if stored:
            payload: dict[str, Any] = {
                "kind": kind.value,
                "summary": summary,
                "subject": mail.subject,
                **details,
            }
            self.queue.enqueue(
                names.REPLY_NOTICE,
                mail.message_id[:200],
                {"job": matched.job_id, **payload},
                priority=Priority.ALERT,
            )
        return stored

    # ---- quiet applications ---------------------------------------------------------------------------

    async def handle_quiet(self, task: TaskRecord) -> None:
        """``quiet_check`` task: follow-up drafts for applications with no reply."""
        try:
            self.check_quiet()
        finally:
            self.schedule_quiet(QUIET_EVERY.total_seconds(), self._other(task.key))

    def check_quiet(self) -> int:
        """Queue follow-up drafts that are due. Returns how many."""
        now, due = self._clock(), 0
        for record in self.applications.sent():
            if not record.submitted_at or self.replies.heard_from(record.job_id):
                continue
            applied_at = from_iso(record.submitted_at)
            if now - applied_at > QUIET_AFTER:
                continue
            count, last = self.applications.reminders(record.job_id)
            since = from_iso(last) if last else applied_at
            if count >= MAX_REMINDERS or now - since < self.followup_after:
                continue
            job = self.jobs.get(record.job_id)
            if job is None:
                continue
            draft = followup_draft(self._name(), job.title, job.company, applied_at.astimezone().date())
            self.applications.reminded(record.job_id)
            self.queue.enqueue(
                names.FOLLOWUP_NOTICE,
                f"{record.job_id}:{count + 1}",
                {"job": record.job_id, "draft": draft, "days": (now - applied_at).days},
                priority=Priority.NORMAL,
            )
            due += 1
        return due
