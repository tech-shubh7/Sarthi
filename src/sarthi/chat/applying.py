"""The application steps in the chat: the filled form for approval, and how each application ended."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from html import escape
from pathlib import Path
from typing import Any

from sarthi.chat.telegram.api import Markup, TelegramApi, buttons
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue import names
from sarthi.queue.tasks import Priority, TaskQueue

log = logging.getLogger(__name__)

PENDING = "apply.pending."  # + job id: {"code", "screenshot"} of a filled form waiting for Submit
MAX_FILLED_SHOWN = 12
MAX_PHOTO_BYTES = 9_000_000  # Telegram's photo limit is 10 MB


def preview_text(company: str, title: str, filled: list[Any]) -> str:
    """The caption under the filled form's screenshot."""
    lines = [f"📝 <b>Ready to apply: {escape(title)}</b> at {escape(company)}", "Filled:"]
    for item in filled[:MAX_FILLED_SHOWN]:
        if isinstance(item, (list, tuple)) and len(item) == 2:  # noqa: PLR2004
            lines.append(f"• {escape(str(item[0])[:60])}: {escape(str(item[1])[:80])}")
    if len(filled) > MAX_FILLED_SHOWN:
        lines.append(f"…and {len(filled) - MAX_FILLED_SHOWN} more")
    lines.append("\nCheck the screenshot, then tap <b>Submit</b>. Nothing is sent until you do.")
    return "\n".join(lines)


class ApplyingFlow:
    """Sends previews and results; turns taps into ``submit`` tasks and status changes."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        api: TelegramApi,
        chats: ChatStore,
        jobs: JobStore,
        applications: ApplicationStore,
        queue: TaskQueue,
        *,
        data_dir: Path,
    ) -> None:
        """Send with ``api``; screenshots are read from ``data_dir``."""
        self.api = api
        self.chats = chats
        self.jobs = jobs
        self.applications = applications
        self.queue = queue
        self.data_dir = data_dir

    def _photo(self, relative: str) -> bytes | None:
        if not relative:
            return None
        path = (self.data_dir / relative).resolve()
        if self.data_dir.resolve() not in path.parents:
            return None  # only files Sarthi wrote into its own data folder
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data if len(data) <= MAX_PHOTO_BYTES else None

    async def preview(self, task: TaskRecord, chat_id: int | None) -> str | None:
        """``apply_preview`` task: the filled form with Submit / Cancel; the message (None: nothing to show).

        The form waits for Submit on the dashboard either way; ``chat_id`` None (no Telegram) sends nothing.
        """
        job = self.jobs.get(task.key)
        code = str(task.payload.get("code") or "")
        if job is None or not code:
            return None
        shot = str(task.payload.get("screenshot") or "")
        # Remembered so the dashboard can show it and submit it too; whichever answers first wins.
        self.chats.set_state(PENDING + task.key, json.dumps({"code": code, "screenshot": shot}))
        caption = preview_text(job.company, job.title, list(task.payload.get("filled") or []))
        if chat_id is None:
            return caption
        markup = buttons([("✅ Submit", f"submit:{task.key}:{code}"), ("❌ Cancel", f"cancel:{task.key}")])
        photo = self._photo(shot)
        if photo is None or not await self._send_photo(chat_id, photo, caption, markup):
            await self.api.send_message(chat_id, caption, markup)
        return caption

    async def _send_photo(self, chat_id: int, photo: bytes, caption: str, markup: Markup | None) -> bool:
        """True when Telegram took the picture (a refused photo must not lose the message)."""
        try:
            await self.api.send_photo(chat_id, photo, caption, markup)
        except Exception as error:  # noqa: BLE001
            log.warning("could not send a screenshot: %s", error)
            return False
        return True

    async def on_button(self, chat_id: int, data: str) -> str:
        """A Submit / Cancel / "I applied" tap. Returns the short reply shown on the button."""
        action, _, rest = data.partition(":")
        job_id, _, code = rest.partition(":")
        if not job_id or self.jobs.get(job_id) is None:
            return "That job is no longer stored."
        if action == "submit" and code:
            newer = self.waiting().get(job_id)
            if newer is not None and newer[0] != code:
                return "This form was filled again: use the Submit under the newest one."  # never voids it
            self.submit(job_id, code)
            return "⏳ Submitting…"
        if action == "cancel":
            self.cancel(job_id)
            return "Cancelled: nothing was sent."
        if action == "applied":
            self.mark_applied(job_id)
            return "✅ Marked as applied"
        return ""

    def waiting(self) -> dict[str, tuple[str, str]]:
        """Filled forms waiting for Submit: job id -> (one-time code, screenshot path)."""
        found = {}
        for job_id, raw in self.chats.states(PENDING).items():
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("code"):
                found[job_id] = (str(data["code"]), str(data.get("screenshot") or ""))
        return found

    def drop_closed(self, still_open: Callable[[str, str], bool]) -> list[str]:
        """Forget filled forms whose page is gone; returns the jobs dropped.

        A restart, 30 idle minutes or a login window closes the page: no Submit is offered that cannot work.
        ``still_open(job, code)`` says whether that fill is still open.
        """
        dropped = [job_id for job_id, (code, _) in self.waiting().items() if not still_open(job_id, code)]
        for job_id in dropped:
            self.chats.set_state(PENDING + job_id, None)
        return dropped

    def submit(self, job_id: str, code: str) -> None:
        """Send the approved form (from a Telegram tap or the dashboard)."""
        self.chats.set_state(PENDING + job_id, None)
        self.queue.enqueue(names.SUBMIT, job_id, {"code": code}, priority=Priority.APPLY)

    def mark_applied(self, job_id: str) -> None:
        """The user applied on the site themselves."""
        self.chats.set_state(PENDING + job_id, None)
        self.applications.record(job_id, ApplicationStatus.APPLIED_BY_USER, detail="applied by the user")
        self.chats.set_job_status(job_id, JobStatus.APPLIED)

    def cancel(self, job_id: str) -> None:
        """Drop the filled form; nothing is sent."""
        self.chats.set_state(PENDING + job_id, None)
        self.applications.record(job_id, ApplicationStatus.FAILED, detail="cancelled by the user")
        self.chats.set_job_status(job_id, JobStatus.SKIPPED)

    async def result(self, task: TaskRecord, chat_id: int | None) -> str | None:
        """``apply_result`` task: how the application ended, with what to do next; the message.

        The job's status changes either way; ``chat_id`` None (no Telegram) sends nothing.
        """
        job = self.jobs.get(task.key)
        if job is None:
            return None
        outcome = str(task.payload.get("outcome") or "")
        detail = escape(str(task.payload.get("detail") or "")[:500])
        what = f"<b>{escape(job.title)}</b> at {escape(job.company)}"
        url = escape(str(task.payload.get("url") or job.apply_url or job.url), quote=True)
        markup: Markup | None = None
        if outcome == "submitted":
            self.chats.set_job_status(task.key, JobStatus.APPLIED)
            text = f"✅ Applied: {what}\n{detail}"
        elif outcome == "duplicate":
            text = f"ℹ️ {detail}"
        elif outcome == "closed":
            text = f"🚫 {what} is no longer taking applications: {detail}"
        elif outcome == "expired" and task.payload.get("approved"):
            text = (
                f"⏳ {what}: {detail} Filling it again. Your Submit still counts: if the new fill is "
                "complete it is sent by itself, otherwise it comes back to you."
            )
        elif outcome == "expired" and task.payload.get("auto_refill"):
            text = f"⏳ {what}: {detail} Sarthi will refill it once and send it if the form is complete."
        elif outcome == "expired":
            text = f"⏳ {what}: {detail} Filling it again for a fresh approval…"
        elif "CAPTCHA" in str(task.payload.get("detail") or ""):
            text = (
                f"🧩 {what} has a CAPTCHA, which only you can solve.\n\n"
                "When you are at your computer, tap <b>Finish it myself</b>: Sarthi opens the form there "
                "with every field filled; you solve the CAPTCHA and press Submit, and Sarthi records it."
            )
            markup = buttons(
                [("🧩 Finish it myself", f"finish:{task.key}")],
                [("✅ I applied myself", f"applied:{task.key}")],
            )
        else:
            text = (
                f"🙋 {what} needs you: {detail}\n\n"
                f'<a href="{url}">Open the application</a>, attach your resume, and submit. '
                "Then tap the button so I can track it."
            )
            markup = buttons([("✅ I applied", f"applied:{task.key}")])
        if chat_id is None:
            return text
        photo = self._photo(str(task.payload.get("screenshot") or "")) if outcome == "submitted" else None
        if photo is None or not await self._send_photo(chat_id, photo, text, markup):
            await self.api.send_message(chat_id, text, markup)
        return text
