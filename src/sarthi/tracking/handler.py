"""Reply tracking: find emails about the user's applications and tell the user; nudge quiet applications.

Each email is read once (by Message-ID). Rules sort most emails (``triage.py``); the AI reads only interviews,
offers, tests and unclear ones. An email counts only when it matches an application Sarthi knows
(``matcher.py``), so a newsletter never becomes "an interview". Quiet applications get at most two
follow-up drafts, the first ``followup_after_days`` after applying, and none after 60 days or once the
employer has written.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sarthi.ai.client import AiClient
from sarthi.ai.replies import read_reply
from sarthi.clock import Clock, from_iso, to_iso, utcnow
from sarthi.db.applications import ApplicationStore
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.db.replies import ReplyRecord, ReplyStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue import names
from sarthi.queue.tasks import PermanentError, Priority, RetryableError, TaskQueue
from sarthi.tracking.followup import followup_draft
from sarthi.tracking.mail import Mail, MailBatch, MailError, MailLoginError, fetch_new
from sarthi.tracking.matcher import Applied, match
from sarthi.tracking.triage import Kind, triage

log = logging.getLogger(__name__)

POSITION_KEY = "mail.position"  # {"box": which mailbox and folder, "validity": UIDVALIDITY, "uid": last read}
STATUS_KEY = "mail.status"  # {"at": ISO time, "ok": bool, "text": the last read's outcome in plain words}
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
    password: str = field(repr=False)  # never in a log line or traceback that prints the mailbox
    folder: str = "INBOX"

    @property
    def identity(self) -> str:
        """Which mailbox and folder: UIDs read in one say nothing about another."""
        return f"{self.user.lower()}@{self.host.lower()}:{self.port}/{self.folder}"


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
        mailbox: Mailbox | Callable[[], Mailbox | None] | None,
        every: timedelta = timedelta(minutes=10),
        followup_after: timedelta = timedelta(days=10),
        name: Callable[[], str] = lambda: "",
        fetch: Callable[..., MailBatch] = fetch_new,
        clock: Clock = utcnow,
    ) -> None:
        """Read ``mailbox`` (None: tracking is off) every ``every``; ``name`` signs follow-up drafts.

        ``mailbox`` may be a function, asked before every read: a mailbox changed on the dashboard counts
        from the next read on, without a restart.
        """
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
        if self.current_mailbox() is not None:
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

    def current_mailbox(self) -> Mailbox | None:
        """The mailbox to read now, or None when tracking is off."""
        box = self.mailbox
        return box() if callable(box) else box

    def position(self, box: Mailbox) -> tuple[int, int]:
        """(UIDVALIDITY, last UID read) in ``box``; (0, 0) for a mailbox or folder not read before."""
        try:
            saved = json.loads(self.chats.get_state(POSITION_KEY) or "{}")
            if saved.get("box") == box.identity:
                return int(saved.get("validity") or 0), int(saved.get("uid") or 0)
        except (ValueError, TypeError, AttributeError):
            pass
        return 0, 0

    def _keep_position(self, box: Mailbox, validity: int, uid: int) -> None:
        state = {"box": box.identity, "validity": validity, "uid": uid}
        self.chats.set_state(POSITION_KEY, json.dumps(state))

    def _keep_status(self, ok: bool, text: str) -> None:
        """The last read's outcome, for the dashboard (never a password: ``MailError`` texts carry none)."""
        state = {"at": to_iso(self._clock()), "ok": ok, "text": text[:300]}
        self.chats.set_state(STATUS_KEY, json.dumps(state))

    async def check_mail(self) -> int:
        """Read new emails; returns how many were replies about applications."""
        box = self.current_mailbox()
        if box is None:
            return 0
        validity, after = self.position(box)
        since = (self._clock() - FIRST_LOOK_BACK).date()
        try:
            batch = await asyncio.to_thread(
                self._fetch, box.host, box.port, box.user, box.password, after,
                since=since, folder=box.folder, validity=validity,
            )  # fmt: skip
        except MailLoginError as error:
            self._keep_status(False, str(error))
            # Not retried at once: failed logins in a row can get the account locked. The next read is due
            # in ``every`` anyway, and a password fixed on the dashboard counts from then.
            raise PermanentError(f"mailbox: {error}") from None
        except MailError as error:
            self._keep_status(False, str(error))
            raise RetryableError(f"mailbox: {error}") from None
        if batch.validity != validity:
            after = 0  # the server renumbered the folder: what was read before is no guide
        applied = self._applied()
        found = 0
        for mail in batch.mails:
            if not self.replies.seen(mail.message_id) and await self._read(mail, applied):
                found += 1
            after = max(after, mail.uid)
            self._keep_position(box, batch.validity, after)  # progress kept even if a later one fails
        self._keep_position(box, batch.validity, max(after, batch.last_uid))
        new = len(batch.mails)
        self._keep_status(True, f"{new} new email{'' if new == 1 else 's'}, {found} about your applications")
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
