"""The Telegram bot: pairing, job alerts with buttons, digests and a few commands.

Only the paired chat is served. Messages from anyone else get no reply (they cannot tell the bot is
running), except a failed pairing attempt.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Any

from sarthi.chat.cards import display_company, job_card
from sarthi.chat.pairing import owner_chat, redeem
from sarthi.chat.telegram.api import Markup, TelegramApi, buttons
from sarthi.clock import Clock, utcnow
from sarthi.db.chat import ANSWERED, ChatStore, JobStatus
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue.tasks import DeferError, PermanentError, Priority, RetryableError, TaskQueue

log = logging.getLogger(__name__)

APPLY_TASK = "apply"
PAUSED_KEY = "alerts.paused"
OFFSET_KEY = "telegram.offset"
WAIT_FOR_PAIRING_S = 600
DIGEST_SIZE = 10
DETAILS_CHARS = 1500
_POLL_ERROR_WAIT_S = 5.0

HELP = (
    "<b>Sarthi</b> finds jobs that fit you and messages you here.\n\n"
    "/today – best matches you have not seen yet\n"
    "/status – what Sarthi has found so far\n"
    "/pause – stop instant alerts (matches wait for you)\n"
    "/resume – start instant alerts again\n"
    "/help – this message"
)

_ACTIONS: dict[str, JobStatus] = {
    "apply": JobStatus.APPLY,
    "skip": JobStatus.SKIPPED,
    "later": JobStatus.LATER,
}
_LABELS: dict[JobStatus, str] = {
    JobStatus.APPLY: "✅ Apply queued",
    JobStatus.SKIPPED: "⏭ Skipped",
    JobStatus.LATER: "🕓 Saved for later",
    JobStatus.APPLIED: "✅ Applied",
}


def job_buttons(job_id: str) -> Markup:
    """Apply / Skip / Later / Details for one job (callback data stays under Telegram's 64 bytes)."""
    return buttons(
        [("✅ Apply", f"apply:{job_id}"), ("⏭ Skip", f"skip:{job_id}")],
        [("🕓 Later", f"later:{job_id}"), ("ℹ️ Details", f"more:{job_id}")],
    )


def _done(label: str) -> Markup:
    return buttons([(label, "noop")])


class TelegramBot:
    """Serves the paired chat and sends alerts and digests."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        api: TelegramApi,
        chats: ChatStore,
        jobs: JobStore,
        matches: MatchStore,
        queue: TaskQueue,
        *,
        clock: Clock = utcnow,
    ) -> None:
        """Send with ``api``; read jobs and matches; record choices in ``chats``; queue applications."""
        self.api = api
        self.chats = chats
        self.jobs = jobs
        self.matches = matches
        self.queue = queue
        self._clock = clock
        self._stopping = asyncio.Event()

    # ---- sending ----------------------------------------------------------------------------------------

    async def send_job(self, chat_id: int, job_id: str, status: JobStatus) -> bool:
        """Send one job card with buttons and record it. False when the job no longer exists."""
        job = self.jobs.get(job_id)
        if job is None:
            return False
        message_id = await self.api.send_message(
            chat_id, job_card(job, self.matches.get(job_id)), job_buttons(job_id)
        )
        self.chats.set_job_status(job_id, status, message_id)
        return True

    async def handle_alert(self, task: TaskRecord) -> None:
        """``alert`` task: send a strong match right away (unless paused or already shown)."""
        chat_id = owner_chat(self.chats)
        if chat_id is None:
            raise DeferError(WAIT_FOR_PAIRING_S)  # not paired yet: keep the alert
        if self.chats.get_state(PAUSED_KEY) or self.chats.job_status(task.key) is not None:
            return  # paused: the match waits for /today or the digest; shown before: never twice
        await self.send_job(chat_id, task.key, JobStatus.ALERTED)

    async def handle_digest(self, task: TaskRecord) -> None:
        """``digest`` task: the best matches not shown yet, in one batch."""
        chat_id = owner_chat(self.chats)
        if chat_id is None:
            raise DeferError(WAIT_FOR_PAIRING_S)
        await self._send_unseen(chat_id, quiet_if_none=True)

    async def _send_unseen(self, chat_id: int, quiet_if_none: bool) -> None:
        items = self.chats.unsent_matches(DIGEST_SIZE)
        if not items:
            if not quiet_if_none:
                await self.api.send_message(
                    chat_id, "No new matches right now. I'll message you when I find one."
                )
            return
        await self.api.send_message(
            chat_id, f"<b>{len(items)} match{'es' if len(items) != 1 else ''} for you</b>"
        )
        for item in items:
            await self.send_job(chat_id, item.job_id, JobStatus.DIGESTED)

    # ---- receiving --------------------------------------------------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> None:
        """React to one message or button tap."""
        if "callback_query" in update:
            await self._on_button(update["callback_query"])
        elif "message" in update:
            await self._on_message(update["message"])

    async def _on_message(self, message: dict[str, Any]) -> None:
        chat_id = int((message.get("chat") or {}).get("id", 0))
        text = str(message.get("text") or "").strip()
        owner = owner_chat(self.chats)
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()

        if command == "/start" and chat_id != owner:
            if argument and redeem(self.chats, argument, chat_id, self._clock()):
                await self.api.send_message(chat_id, "✅ Paired. I'll send your job matches here.\n\n" + HELP)
            elif argument:
                await self.api.send_message(
                    chat_id, "Pairing failed. Run <code>sarthi pair</code> for a new code."
                )
            return
        if chat_id != owner:
            return  # strangers get no reply

        if command in ("/start", "/help"):
            await self.api.send_message(chat_id, HELP)
        elif command == "/status":
            await self.api.send_message(chat_id, self._status_text())
        elif command == "/pause":
            self.chats.set_state(PAUSED_KEY, "1")
            await self.api.send_message(
                chat_id, "⏸ Instant alerts paused. Matches wait for you: /today or /resume."
            )
        elif command == "/resume":
            self.chats.set_state(PAUSED_KEY, None)
            waiting = len(self.chats.unsent_matches(DIGEST_SIZE))
            await self.api.send_message(chat_id, f"▶️ Instant alerts on. {waiting} match(es) waiting: /today")
        elif command == "/today":
            await self._send_unseen(chat_id, quiet_if_none=False)
        else:
            await self.api.send_message(chat_id, "I didn't understand that. /help lists what I can do.")

    async def _on_button(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id", ""))
        chat_id = int(((callback.get("message") or {}).get("chat") or {}).get("id", 0))
        message_id = int((callback.get("message") or {}).get("message_id", 0))
        if chat_id != owner_chat(self.chats):
            await self.api.answer_callback(callback_id)
            return
        action, _, job_id = str(callback.get("data") or "").partition(":")
        if action == "more":
            await self.api.answer_callback(callback_id)
            await self._send_details(chat_id, job_id)
            return
        if action not in _ACTIONS:
            await self.api.answer_callback(callback_id)
            return
        current = self.chats.job_status(job_id)
        if current not in ANSWERED:  # the first answer counts; a second tap changes nothing
            current = _ACTIONS[action]
            self.chats.set_job_status(job_id, current)
            if current is JobStatus.APPLY:
                self.queue.enqueue(APPLY_TASK, job_id, priority=Priority.NORMAL)
        label = _LABELS[current]
        await self.api.answer_callback(callback_id, label)
        await self.api.edit_buttons(chat_id, message_id, _done(label))

    async def _send_details(self, chat_id: int, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            await self.api.send_message(chat_id, "That job is no longer stored.")
            return
        text = (self.jobs.full_text(job_id) or job.description).strip()
        excerpt = text[:DETAILS_CHARS] + ("…" if len(text) > DETAILS_CHARS else "")
        header = f"<b>{escape(job.title)}</b> · {escape(display_company(job.company))}"
        await self.api.send_message(chat_id, f"{header}\n\n{escape(excerpt) or 'No description was given.'}")

    def _status_text(self) -> str:
        found = self.jobs.count()
        decided = self.matches.counts()
        yours = self.chats.status_counts()
        paused = " (alerts paused)" if self.chats.get_state(PAUSED_KEY) else ""
        return (
            f"<b>Sarthi status</b>{paused}\n"
            f"Jobs found: {found}\n"
            f"Strong matches: {decided.get('alert', 0)} · fair: {decided.get('digest', 0)} · "
            f"hidden: {decided.get('hide', 0)}\n"
            f"You chose: apply {yours.get('apply', 0)} · skip {yours.get('skipped', 0)} · "
            f"later {yours.get('later', 0)}"
        )

    # ---- polling ----------------------------------------------------------------------------------------

    def stop(self) -> None:
        """Stop polling after the current request."""
        self._stopping.set()

    async def poll(self) -> None:
        """Receive updates until ``stop()``. A bad token stops the bot; network trouble is waited out."""
        offset_text = self.chats.get_state(OFFSET_KEY)
        offset = int(offset_text) if offset_text else None
        while not self._stopping.is_set():
            try:
                updates = await self.api.get_updates(offset)
            except RetryableError as error:
                log.warning("telegram: %s; retrying", error)
                await asyncio.sleep(error.retry_after or _POLL_ERROR_WAIT_S)
                continue
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    await self.handle_update(update)
                except (RetryableError, PermanentError) as error:
                    log.warning("telegram: could not handle an update: %s", error)
                self.chats.set_state(OFFSET_KEY, str(offset))  # never handle the same update twice
