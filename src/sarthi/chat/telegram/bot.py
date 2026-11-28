"""The Telegram bot: pairing, job alerts with buttons, digests and a few commands.

Only the paired chat is served. Messages from anyone else get no reply (they cannot tell the bot is
running), except a failed pairing attempt.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from html import escape
from pathlib import Path
from typing import Any

from sarthi.ai.intent import Kind
from sarthi.chat.applying import ApplyingFlow
from sarthi.chat.autoapply import AutoApply, AutoApplySettings
from sarthi.chat.cards import display_company, job_card
from sarthi.chat.catchup import ASK as ASK_CATCHUP
from sarthi.chat.catchup import CatchUp, window_buttons
from sarthi.chat.document_report import report_text
from sarthi.chat.inbox import Inbox
from sarthi.chat.pairing import owner_chat, redeem
from sarthi.chat.questions import QuestionFlow
from sarthi.chat.setup import WHY, SetupFlow
from sarthi.chat.source_notice import source_notice_text
from sarthi.chat.telegram.api import Markup, TelegramApi, buttons
from sarthi.chat.telegram.brand import brand_bot
from sarthi.chat.tracking import followup_text, reply_text
from sarthi.chat.understand import Route, route, route_by_rules
from sarthi.clock import Clock, to_iso, utcnow
from sarthi.db.answers import AnswerStore
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.chat import ANSWERED, ChatStore, JobStatus
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchStore
from sarthi.db.notices import NoticeStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue import names
from sarthi.queue.tasks import PermanentError, Priority, RetryableError, TaskQueue

log = logging.getLogger(__name__)

APPLY_TASK = names.APPLY
# Every step of an application, from the documents to the result: one waiting or running means it is alive.
APPLY_STEPS = (
    names.DOCUMENTS, names.DOCUMENTS_READY, names.FORM_CHECK, names.ASK_QUESTIONS, names.APPLY,
    names.AUTO_APPLY, names.APPLY_PREVIEW, names.SUBMIT, names.APPLY_RESULT,
)  # fmt: skip
OTHER_APPLY_STEPS = tuple(step for step in APPLY_STEPS if step != names.AUTO_APPLY)
PAUSED_KEY = "alerts.paused"
OFFSET_KEY = "telegram.offset"
WAIT_FOR_PAIRING_S = 600
DIGEST_SIZE = 10
# Instant alerts per hour. A first run (or a new profile) can find dozens of strong matches at once: the phone
# buzzes for the best few, the rest wait in /today and the digest, and one message says so.
ALERTS_PER_HOUR = 5
CAPPED_KEY = "alerts.capped_at"
REUSED_TEXT = "📄 Applying with the tailored resume and cover letter I sent you for this job."
CAPPED_TEXT = (
    "⭐ More strong matches are waiting. To keep your phone quiet I send at most {n} an hour: "
    "see them all with /today."
)
DETAILS_CHARS = 1500
_POLL_ERROR_WAIT_S = 5.0

PAIR_HOW = (
    "👋 Hi! To link this chat to your Sarthi, run this on your computer:\n\n"
    "<code>uv run sarthi pair</code>\n\nthen send me the code it shows."
)
PAIR_FAILED = "That code didn't work (wrong or expired). Run <code>uv run sarthi pair</code> for a new one."

BUSY_TEXT = "⏳ Still reading your last message — I'll take this one right after."
_THANKS = ("thank", "thx", "ty")
_WAIT = ("wait", "hold", "one sec", "one min", "one moment", "hmm")
_BYE = ("bye", "see you")


_SMALLTALK_REPLIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (_THANKS, "You're welcome! 🙂"),
    (_WAIT, "Sure, take your time."),
    (_BYE, "Bye! I'll keep looking for jobs for you."),
)
NOT_SURE = (
    "I'm not sure what you mean. You can change your profile just by telling me, "
    "e.g. <i>add UAE to my countries</i> — or see /help."
)


def _smalltalk_reply(text: str, *, in_setup: bool) -> str:
    lowered = text.lower()
    for words, reply in _SMALLTALK_REPLIES:
        if any(word in lowered for word in words):
            return reply
    return "Hi! 👋" if in_setup else "Hi! 👋 I'm watching new jobs for you. /today shows your best matches."


def reply_for(decided: Route, text: str, *, in_setup: bool) -> str:
    """A short reply to small talk, a side question or an off-topic message."""
    if decided.kind is Kind.SMALLTALK:
        return _smalltalk_reply(text, in_setup=in_setup)
    if decided.kind is Kind.QUESTION and decided.field is not None:
        return f"ℹ️ {WHY[decided.field]}"
    if decided.kind is Kind.QUESTION:
        return "ℹ️ I'm setting up your job profile so I only send you jobs that fit." if in_setup else HELP
    if decided.kind is Kind.ANSWER:  # outside setup nothing was asked
        return NOT_SURE
    return "🙂 I can only help with your job search." + ("" if in_setup else " /help shows what I can do.")


# The one list of commands: shown in /help and in Telegram's "/" menu (setMyCommands).
COMMANDS: tuple[tuple[str, str], ...] = (
    ("setup", "Create or change your profile (resume + a few questions)"),
    ("profile", "Show your profile (change anything by just telling me)"),
    ("today", "Best matches you have not seen yet"),
    ("catchup", "Everything posted in the last 24 hours to 7 days, in one batch"),
    ("status", "What Sarthi has found so far"),
    ("pause", "Stop instant alerts (matches wait for you)"),
    ("resume", "Start instant alerts again"),
    ("cancel", "Stop the profile setup you are in"),
    ("login", "Log in to a job site once (LinkedIn, Naukri, Indeed, ...), so Sarthi can apply there"),
    ("autoapply", "Apply by itself to matches above your line, after a 5-minute veto (e.g. /autoapply 85)"),
    ("help", "What Sarthi does and these commands"),
)
# Sites /login offers (the apply step has their login pages): (key, name), two to a row.
LOGIN_SITES: tuple[tuple[str, str], ...] = (
    ("linkedin", "LinkedIn"),
    ("naukri", "Naukri"),
    ("indeed", "Indeed"),
    ("instahyre", "Instahyre"),
    ("wellfound", "Wellfound"),
    ("foundit", "foundit"),
    ("glassdoor", "Glassdoor"),
    ("shine", "Shine"),
    ("hirist", "Hirist"),
)
HELP = "<b>Sarthi</b> finds jobs that fit you and messages you here.\n\n" + "\n".join(
    f"/{name} – {about[0].lower()}{about[1:]}" for name, about in COMMANDS
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
    """Apply / Skip / Later / Details / Resume for one job (callback data stays under Telegram's 64 bytes)."""
    return buttons(
        [("✅ Apply", f"apply:{job_id}"), ("⏭ Skip", f"skip:{job_id}")],
        [("🕓 Later", f"later:{job_id}"), ("ℹ️ Details", f"more:{job_id}")],
        [("📄 Tailored resume", f"docs:{job_id}")],
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
        setup: SetupFlow | None = None,
        group_messages: bool = False,
        profile_version: Callable[[], str | None] = lambda: None,
        documents: DocumentStore | None = None,
        answers: AnswerStore | None = None,
        applications: ApplicationStore | None = None,
        data_dir: Path | None = None,
        auto_apply: AutoApplySettings | None = None,
        notices: NoticeStore | None = None,
        telegram: bool = True,
        clock: Clock = utcnow,
    ) -> None:
        """Send with ``api``; read jobs and matches; record choices in ``chats``; queue applications.

        ``telegram`` False: no bot is set up, nothing is sent (the dashboard shows everything from
        ``notices``); a chat paired earlier waits for the bot's return.
        """
        self.telegram = telegram
        self.notices = notices
        self.setup = setup
        self.inbox = Inbox(self._handle_batch, self._busy_notice) if group_messages else None
        self.api = api
        self.chats = chats
        self.jobs = jobs
        self.matches = matches
        self.queue = queue
        self._clock = clock
        self._profile_version = profile_version
        self.documents = documents
        self.data_dir = data_dir
        self.questions = QuestionFlow(api, chats, jobs, answers, queue) if answers is not None else None
        self.applying = (
            ApplyingFlow(api, chats, jobs, applications, queue, data_dir=data_dir)
            if applications is not None and data_dir is not None
            else None
        )
        # Opens a job site's login page (set by the app): (chat id, site) -> None; it reports back itself.
        self.site_login: Callable[[int, str], Awaitable[None]] | None = None
        # Opens a filled form on the user's screen to finish by hand (a CAPTCHA): (chat id, job id) -> None.
        self.finish_by_hand: Callable[[int, str], Awaitable[None]] | None = None
        self._stopping = asyncio.Event()
        self.auto = AutoApply(chats, jobs, queue, auto_apply or AutoApplySettings(), clock=clock)
        self.catchup = CatchUp(api, chats, jobs, matches, queue, send_job=self.send_job, clock=clock)

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

    def chat(self) -> int | None:
        """The paired chat to send to, or None: no Telegram set up, or not paired (yet).

        Without a chat every pipeline step still runs; what would be sent is kept as a notice and shown on
        the dashboard instead (it shows the notices either way).
        """
        return owner_chat(self.chats) if self.telegram else None

    def note(self, kind: str, text: str, job_id: str | None = None) -> None:
        """Keep a message for the dashboard's Activity page."""
        if self.notices is not None:
            self.notices.add(kind, text, job_id)

    async def handle_alert(self, task: TaskRecord) -> None:
        """``alert`` task: send a strong match right away (unless paused or already shown).

        Without a chat the match waits on the dashboard (and in a later digest); auto-apply is still
        scheduled, its veto window shown on the dashboard's Activity page.
        """
        chat_id = self.chat()
        if self.chats.get_state(PAUSED_KEY) or self.chats.job_status(task.key) is not None:
            return  # paused: the match waits for /today or the digest; shown before: never twice
        if not self._still_strong(task.key):
            return  # re-matched since (e.g. the profile changed): the alert is out of date
        if self.catchup.running():
            return  # a catch-up is collecting results: this match comes in its batch
        match = self.matches.get(task.key)
        if match is not None and self.auto.wanted(match):
            if chat_id is None:
                if self.jobs.get(task.key) is not None:
                    text, _ = self.auto.schedule(task.key, match.score)
                    self.note("auto_apply", text, task.key)
            elif await self.send_job(chat_id, task.key, JobStatus.ALERTED):
                text, markup = self.auto.schedule(task.key, match.score)
                await self.api.send_message(chat_id, text, markup)
                self.note("auto_apply", text, task.key)
            return
        if chat_id is None:
            return  # no chat: the dashboard lists it with the other strong matches
        hour_ago = self._clock() - timedelta(hours=1)
        if self.chats.alerted_since(hour_ago) >= ALERTS_PER_HOUR:
            noted = self.chats.get_state(CAPPED_KEY)
            if noted is None or noted < to_iso(hour_ago):  # once an hour at most
                self.chats.set_state(CAPPED_KEY, to_iso(self._clock()))
                await self.api.send_message(chat_id, CAPPED_TEXT.format(n=ALERTS_PER_HOUR))
            return  # the match stays unsent: /today and the digest show it
        await self.send_job(chat_id, task.key, JobStatus.ALERTED)

    async def handle_auto_apply(self, task: TaskRecord) -> None:
        """Apply after the veto window while the job still qualifies."""
        current = self.chats.job_status(task.key)
        resumed = (
            task.payload.get("resume") is True
            and current is JobStatus.APPLY
            and not self.chats.applying_alive(task.key, OTHER_APPLY_STEPS)
        )
        if not self.chats.is_auto(task.key) or (current in ANSWERED and not resumed):
            return
        if not self._still_strong(task.key) or not self.auto.qualifies(self.matches.get(task.key)):
            self.auto.cancel(task.key)
            return
        self._start_applying(task.key)

    def apply_now(self, job_id: str) -> bool:
        """Apply now to a job waiting out its auto-apply veto window. False when none is waiting."""
        current = self.chats.job_status(job_id)
        recoverable = current is JobStatus.APPLY and not self.chats.applying_alive(job_id, OTHER_APPLY_STEPS)
        if not self.chats.is_auto(job_id) or (current in ANSWERED and not recoverable):
            return False
        if not self._still_strong(job_id) or not self.auto.qualifies(self.matches.get(job_id)):
            self.auto.cancel(job_id)
            return False
        self._start_applying(job_id)
        return True

    def _start_applying(self, job_id: str) -> None:
        self.chats.set_job_status(job_id, JobStatus.APPLY)
        self.queue.enqueue(names.DOCUMENTS, job_id, {"apply": True}, priority=Priority.ALERT)

    def _still_strong(self, job_id: str) -> bool:
        """True when the job's current match is an alert made with the current profile."""
        match = self.matches.get(job_id)
        if match is None or match.decision != "alert":
            return False
        current = self._profile_version()
        return current is None or match.profile_version == current

    async def handle_documents_ready(self, task: TaskRecord) -> None:
        """``documents_ready`` task: send the tailored resume, cover letter and keyword report.

        The application's next step (the form check) is queued whether or not a chat gets the files: on the
        dashboard they are on the Applications page.
        """
        chat_id = self.chat()
        error = task.payload.get("error")
        apply = bool(task.payload.get("apply"))
        if task.payload.get("reused") and apply:
            self.queue.enqueue(names.FORM_CHECK, task.key, priority=Priority.APPLY)
            await self._deliver(chat_id, REUSED_TEXT)
            return
        record = self.documents.get(task.key) if self.documents is not None else None
        missing = (
            record is not None
            and self.data_dir is not None
            and not all((self.data_dir / p).is_file() for p in (record.resume_file, record.letter_file))
        )
        if error or record is None or self.data_dir is None or missing:
            why = str(
                error
                or ("The document files are missing." if missing else "The documents could not be made.")
            )
            text = f"📄 {escape(why)}" + (" Tap Apply again to retry." if apply else "")
            if apply and self.applying is not None:  # the application stops here: the user sees why
                self.applying.applications.record(
                    task.key,
                    ApplicationStatus.FAILED,
                    detail=f"The tailored documents could not be made: {why}",
                )
            self.note("documents", text, task.key)
            await self._deliver(chat_id, text)
            return
        if apply:  # next: the form's questions, then the application itself (Telegram or not)
            self.queue.enqueue(names.FORM_CHECK, task.key, priority=Priority.APPLY)
        job = self.jobs.get(task.key)
        what = f"{job.title} at {job.company}" if job else "this job"
        self.note("documents", f"📄 Tailored resume and cover letter ready for {what}.", task.key)
        if chat_id is not None:
            try:
                await self._send_documents(chat_id, record.resume_file, record.letter_file)
                await self.api.send_message(chat_id, report_text(record.report))
            except (RetryableError, PermanentError) as failure:  # delivery only: the application goes on
                log.warning("telegram: could not send the documents: %s", failure)

    async def _deliver(self, chat_id: int | None, text: str) -> None:
        """Send news to the chat when there is one; a failed send never stops the pipeline step."""
        if chat_id is None:
            return
        try:
            await self.api.send_message(chat_id, text)
        except (RetryableError, PermanentError) as failure:
            log.warning("telegram: could not send a message: %s", failure)

    async def _send_documents(self, chat_id: int, *paths: str) -> bool:
        """Send the PDFs; False (and why) when a file is missing."""
        assert self.data_dir is not None  # noqa: S101  (checked by the caller)
        for path in paths:
            file = self.data_dir / path
            try:
                content = file.read_bytes()
            except OSError:
                await self.api.send_message(
                    chat_id, "📄 The document files are missing; tap the button again."
                )
                return False
            await self.api.send_document(chat_id, file.name, content, mime="application/pdf")
        return True

    async def handle_ask_questions(self, task: TaskRecord) -> None:
        """``ask_questions`` task: ask the next question a job's form needs (in the chat and/or dashboard)."""
        if self.questions is None:
            return
        chat_id = self.chat()
        left = len(self.questions.answers.pending(task.key))
        await self.questions.handle(task, chat_id)
        if left and chat_id is None:
            job = self.jobs.get(task.key)
            who = job.company if job else "The employer"
            self.note("questions", f"📝 {who} asks {left} question(s) only you can answer.", task.key)

    async def handle_apply_preview(self, task: TaskRecord) -> None:
        """``apply_preview`` task: the filled form, waiting for Submit (in the chat and on the dashboard)."""
        if self.applying is not None and (text := await self.applying.preview(task, self.chat())):
            self.note("preview", text, task.key)

    async def handle_apply_result(self, task: TaskRecord) -> None:
        """``apply_result`` task: how an application ended."""
        if self.applying is not None and (text := await self.applying.result(task, self.chat())):
            self.note("result", text, task.key)

    async def _tell(self, kind: str, text: str, job_id: str | None = None) -> None:
        """A message that is only news: sent to the chat when there is one, always kept for the dashboard."""
        chat_id = self.chat()
        if chat_id is not None:
            await self.api.send_message(chat_id, text)
        self.note(kind, text, job_id)

    async def handle_reply_notice(self, task: TaskRecord) -> None:
        """``reply_notice`` task: an employer replied about an application."""
        job_id = str(task.payload.get("job") or "")
        await self._tell("reply", reply_text(self.jobs.get(job_id), task.payload), job_id or None)

    async def handle_followup_notice(self, task: TaskRecord) -> None:
        """``followup_notice`` task: a follow-up draft for a quiet application."""
        job_id = str(task.payload.get("job") or "")
        await self._tell("followup", followup_text(self.jobs.get(job_id), task.payload), job_id or None)

    async def handle_source_notice(self, task: TaskRecord) -> None:
        """``source_notice`` task: a job source stopped (or started again) working."""
        await self._tell("source", source_notice_text(task.payload))

    async def handle_catchup_report(self, task: TaskRecord) -> None:
        """``catchup_report`` task: send the catch-up batch (without a chat: matches are on the dashboard)."""
        self.note("catchup", await self.catchup.handle_report(task, self.chat()))

    async def handle_digest(self, task: TaskRecord) -> None:
        """``digest`` task: the best matches not shown yet, in one batch (without a chat: the dashboard)."""
        chat_id = self.chat()
        if chat_id is not None:
            await self._send_unseen(chat_id, quiet_if_none=True)

    async def _send_unseen(self, chat_id: int, quiet_if_none: bool) -> None:
        items = self.chats.unsent_matches(DIGEST_SIZE, self._profile_version())
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

        if owner is None:
            await self._pair(chat_id, argument if command == "/start" else text)
            return
        if command == "/start" and argument and chat_id != owner:
            await self._pair(chat_id, argument, quiet=True)  # moving to a new chat after `sarthi pair`
            return
        if chat_id != owner:
            return  # strangers get no reply
        if message.get("document") or (text and not text.startswith("/")):
            if self.inbox is not None:
                await self.inbox.put(chat_id, message)  # grouped and handled in the background
            else:
                await self._handle_batch(chat_id, [message])
            return
        if command == "/autoapply":
            await self.api.send_message(chat_id, self.auto.set_line(argument))
            return
        if not await self._on_setup_command(chat_id, command):
            await self._on_command(chat_id, command)

    async def _on_command(self, chat_id: int, command: str) -> None:
        """/help, /status, /pause, /resume, /today."""
        if command == "/catchup":
            await self.api.send_message(chat_id, ASK_CATCHUP, window_buttons())
        elif command in ("/start", "/help"):
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
            waiting = len(self.chats.unsent_matches(DIGEST_SIZE, self._profile_version()))
            await self.api.send_message(chat_id, f"▶️ Instant alerts on. {waiting} match(es) waiting: /today")
        elif command == "/today":
            await self._send_unseen(chat_id, quiet_if_none=False)
        elif command == "/login":
            await self.api.send_message(
                chat_id,
                "Which site? You log in on the site itself; Sarthi never sees your password.",
                buttons(
                    *[
                        [(name, f"login:{key}") for key, name in LOGIN_SITES[i : i + 2]]
                        for i in range(0, len(LOGIN_SITES), 2)
                    ]
                ),
            )
        else:
            await self.api.send_message(chat_id, "I didn't understand that. /help lists what I can do.")

    async def _on_setup_command(self, chat_id: int, command: str) -> bool:
        """/setup, /profile, /cancel. True when handled here."""
        if self.setup is None:
            return False
        if command == "/setup":
            await self.setup.start(chat_id)
        elif command == "/profile":
            await self.setup.show(chat_id)
        elif command == "/cancel" and self.setup.active:
            await self.setup.cancel(chat_id)
        else:
            return False
        return True

    async def _busy_notice(self, chat_id: int) -> None:
        await self.api.send_message(chat_id, BUSY_TEXT)

    async def _handle_batch(self, chat_id: int, messages: list[dict[str, Any]]) -> None:
        """Files first, then all text of a quick burst read as one message."""
        texts: list[str] = []
        for message in messages:
            if message.get("document") and self.setup is not None:
                await self.setup.on_document(chat_id, message["document"])
            elif text := str(message.get("text") or "").strip():
                texts.append(text)
        if not texts:
            return
        joined = "\n".join(texts)
        setup_active = self.setup is not None and self.setup.active
        if not setup_active and self.questions is not None and await self.questions.on_text(chat_id, joined):
            return  # an answer to an application question
        await self._converse(chat_id, joined)

    async def _converse(self, chat_id: int, text: str) -> None:
        """Work out what the message is, then answer, correct, explain or chat back."""
        setup = self.setup
        if setup is None:
            await self.api.send_message(chat_id, "I didn't understand that. /help lists what I can do.")
            return
        in_setup = setup.active
        if in_setup and setup.asking is None:  # resume or summary step: no profile field is being asked
            decided = route_by_rules(text, None)
            if decided is None or decided.kind is Kind.ANSWER:
                await setup.on_text(chat_id, text)
                return
        else:
            decided = await route(setup.ai, text, setup.asking, setup.question_text())
            if decided.kind is Kind.CORRECTION and setup.fits_current_question(text):
                decided = Route(
                    Kind.ANSWER, setup.asking
                )  # it answers what was asked; not a change elsewhere
        if decided.kind is Kind.ANSWER and in_setup:
            await setup.on_text(chat_id, text)
        elif decided.kind is Kind.CORRECTION and decided.field is not None:
            await setup.correct(chat_id, decided.field, text)
        else:
            await self.api.send_message(chat_id, reply_for(decided, text, in_setup=in_setup))
            if in_setup:
                await setup.ask_again(chat_id)

    async def _pair(self, chat_id: int, code: str, *, quiet: bool = False) -> None:
        """Try ``code``; before anyone is paired, explain how to pair instead of staying silent.

        ``quiet`` (after pairing): a failed attempt gets no reply, so strangers learn nothing.
        """
        if code and not code.startswith("/"):
            if redeem(self.chats, code, chat_id, self._clock()):
                await self.api.send_message(
                    chat_id, "✅ Paired. Next: send /setup to create your profile.\n\n" + HELP
                )
            elif not quiet:
                await self.api.send_message(chat_id, PAIR_FAILED)
        elif owner_chat(self.chats) is None:
            await self.api.send_message(chat_id, PAIR_HOW)

    async def _on_button(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id", ""))
        chat_id = int(((callback.get("message") or {}).get("chat") or {}).get("id", 0))
        message_id = int((callback.get("message") or {}).get("message_id", 0))
        if chat_id != owner_chat(self.chats):
            await self.api.answer_callback(callback_id)
            return
        data = str(callback.get("data") or "")
        if await self._on_flow_button(callback_id, chat_id, message_id, data):
            return
        action, _, job_id = data.partition(":")
        if action == "docs":
            await self.api.answer_callback(callback_id, "📄 Making your tailored resume…")
            self.queue.enqueue(names.DOCUMENTS, job_id, {"apply": False}, priority=Priority.ALERT)
            return
        if action == "more":
            await self.api.answer_callback(callback_id)
            await self._send_details(chat_id, job_id)
            return
        if action not in _ACTIONS:
            await self.api.answer_callback(callback_id)
            return
        label = _LABELS[self.choose(job_id, action)]
        await self.api.answer_callback(callback_id, label)
        await self.api.edit_buttons(chat_id, message_id, _done(label))

    def choose(self, job_id: str, action: str) -> JobStatus:
        """Apply, skip or later on one job (its buttons here or on the dashboard); the job's status after.

        The first answer counts, so a second tap changes nothing; only a job saved for later can still be
        applied to. The user's own choice ends any auto-apply waiting for this job.
        """
        current = self.chats.job_status(job_id)
        wanted = _ACTIONS[action]
        if current not in ANSWERED or (current is JobStatus.LATER and wanted is JobStatus.APPLY):
            current = wanted
            self.chats.set_job_status(job_id, current)
            # The user's own tap: sent by itself when they chose so (/autoapply taps on), else the preview.
            self.chats.set_auto(job_id, on=self.auto.taps_send())
            if current is JobStatus.APPLY:  # documents first; the application follows them
                self.queue.enqueue(names.DOCUMENTS, job_id, {"apply": True}, priority=Priority.ALERT)
        elif (
            current is JobStatus.APPLY
            and wanted is JobStatus.APPLY
            and not self.chats.applying_alive(job_id, APPLY_STEPS)
        ):  # an Apply whose steps died (or whose documents failed): a new tap starts it again
            self.chats.set_auto(job_id, on=self.auto.taps_send())
            self.queue.enqueue(names.DOCUMENTS, job_id, {"apply": True}, priority=Priority.ALERT)
        return current

    async def _on_window_button(self, callback_id: str, chat_id: int, message_id: int, data: str) -> None:
        """Buttons that open a browser on the user's computer: a site login, or a CAPTCHA form to finish."""
        kind, _, value = data.partition(":")
        await self.api.answer_callback(
            callback_id, "Opening the form on your computer..." if kind == "finish" else ""
        )
        await self.api.edit_buttons(chat_id, message_id, None)
        if kind == "login":
            if self.site_login is None:
                await self.api.send_message(
                    chat_id, "Logging in from here is not available; run sarthi login."
                )
            else:
                await self.site_login(chat_id, value)
        elif self.finish_by_hand is None:
            await self.api.send_message(chat_id, "Finishing a form from here is not available right now.")
        else:
            await self.finish_by_hand(chat_id, value)

    async def _on_flow_button(self, callback_id: str, chat_id: int, message_id: int, data: str) -> bool:
        """Buttons of the setup and catch-up conversations. True when handled here."""
        if data == "profile:undo" and self.setup is not None:
            await self.api.answer_callback(callback_id)
            await self.api.edit_buttons(chat_id, message_id, None)
            await self.setup.undo(chat_id)
        elif data.startswith("setup:pick:") and self.setup is not None:
            await self.api.answer_callback(callback_id)
            await self.setup.on_button(chat_id, data, message_id)  # the picker updates its own buttons
        elif data.startswith("setup:") and self.setup is not None:
            await self.api.answer_callback(callback_id)
            await self.api.edit_buttons(chat_id, message_id, None)  # one tap per question
            await self.setup.on_button(chat_id, data, message_id)
        elif data.startswith("ans:") and self.questions is not None:
            await self.api.answer_callback(callback_id)
            await self.api.edit_buttons(chat_id, message_id, None)
            await self.questions.on_button(chat_id, data)
        elif data in {f"login:{key}" for key, _ in LOGIN_SITES} or data.startswith("finish:"):
            await self._on_window_button(callback_id, chat_id, message_id, data)
        elif data.split(":", 1)[0] in ("submit", "cancel", "applied") and self.applying is not None:
            label = await self.applying.on_button(chat_id, data)
            await self.api.answer_callback(callback_id, label)
            await self.api.edit_buttons(chat_id, message_id, _done(label) if label else None)
        elif data.startswith("auto:"):
            await self._on_auto_button(callback_id, chat_id, message_id, data)
        elif data.startswith("catchup:"):
            await self.api.answer_callback(callback_id)
            await self.api.edit_buttons(chat_id, message_id, None)
            await self.catchup.on_button(chat_id, data)
        elif data.startswith("batch:"):
            await self.api.answer_callback(callback_id)
            await self.api.edit_buttons(chat_id, message_id, None)  # one tap per catch-up
            await self.catchup.on_batch(chat_id, data)
        else:
            return False
        return True

    async def _on_auto_button(self, callback_id: str, chat_id: int, message_id: int, data: str) -> None:
        """Cancel or Apply now under an auto-apply notice."""
        _, action, job_id = [*data.split(":", 2), "", ""][:3]
        label = ""
        if action == "cancel":
            self.auto.cancel(job_id)
            label = "Auto-apply cancelled"
        elif action == "now" and self.apply_now(job_id):
            label = "🚀 Applying now"
        await self.api.answer_callback(callback_id, label)
        await self.api.edit_buttons(chat_id, message_id, _done(label) if label else None)

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
            f"Applied: {yours.get('applied', 0)} · applying now: {yours.get('apply', 0)}\n"
            f"You chose: skip {yours.get('skipped', 0)} · later {yours.get('later', 0)}"
        )

    # ---- polling ----------------------------------------------------------------------------------------

    def stop(self) -> None:
        """Stop polling after the current request."""
        self._stopping.set()

    async def poll(self) -> None:
        """Receive updates until ``stop()``. A bad token stops the bot; network trouble is waited out."""
        try:
            await self.api.set_commands(COMMANDS)  # the menu that opens when the user types "/"
        except (RetryableError, PermanentError) as error:
            log.warning("telegram: could not set the command menu: %s", error)
        await brand_bot(self.api, self.chats)  # the logo and description, once per bot
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
