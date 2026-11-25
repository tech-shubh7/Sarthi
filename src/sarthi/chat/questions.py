"""Asking the user the application questions only they can answer, one at a time, in the chat.

A question with a short list of answers gets one button per answer; any other is answered by typing. An
answer must fit the question (one of its options, a real date for a date): otherwise the question is asked
again with the reason. Every answer goes into the answer bank, so the same question is never asked twice.
When a job has no questions left, its application continues.
"""

from __future__ import annotations

import json
import re
from html import escape

from sarthi.chat.telegram.api import Markup, TelegramApi, buttons
from sarthi.db.answers import MAX_ANSWER_CHARS, AnswerStore
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.jobs.forms import FormField, best_option, company_specific, for_job, pick_options, to_date
from sarthi.queue import names
from sarthi.queue.tasks import Priority, TaskQueue

ASKING_KEY = "form.asking"  # JSON {"job": id, "key": field key} while a question waits for a typed answer
MAX_BUTTONS = 10
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def clean_answer(text: str) -> str:
    """The user's typed answer without control characters, trimmed to the stored length."""
    return re.sub(r"[ \t]+", " ", _CONTROL.sub("", text)).strip()[:MAX_ANSWER_CHARS]


def fitted(question: FormField, text: str) -> tuple[str | None, str]:
    """(the answer to store, "") or (None, why it does not fit)."""
    answer = clean_answer(text)
    if not answer:
        return None, "Please type an answer."
    if question.type == "date" and to_date(answer) is None:
        return None, "Please give a date (e.g. 15 Dec 2026) or a time from now (e.g. 2 weeks)."
    if question.options and question.type == "checkbox" and len(question.options) > 1:
        listed = answer
        if not any(re.search(r"\band\b|&", o, re.I) for o in question.options):
            listed = re.sub(r"\s+(?:and|&)\s+", ", ", answer, flags=re.I)  # "Python and Go" lists two
        picks = pick_options(question.options, listed)
        return (", ".join(picks), "") if picks else (None, "Please pick from the listed answers.")
    if question.options:
        option = best_option(question.options, answer)
        return (option, "") if option else (None, "Please pick one of the listed answers.")
    return answer, ""


class QuestionFlow:
    """Asks a job's pending questions and continues the application when none are left."""

    def __init__(
        self, api: TelegramApi, chats: ChatStore, jobs: JobStore, answers: AnswerStore, queue: TaskQueue
    ) -> None:
        """Send with ``api``; questions and answers live in ``answers``."""
        self.api = api
        self.chats = chats
        self.jobs = jobs
        self.answers = answers
        self.queue = queue

    def waiting(self) -> tuple[str, str] | None:
        """(job id, field key) of the question waiting for a typed answer, or None."""
        raw = self.chats.get_state(ASKING_KEY)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("job"), str)
            or not isinstance(data.get("key"), str)
        ):
            return None
        return data["job"], data["key"]

    def _question(self, job_id: str, key: str) -> FormField | None:
        return next((q for q in self.answers.pending(job_id) if q.key == key), None)

    async def handle(self, task: TaskRecord, chat_id: int | None) -> None:
        """``ask_questions`` task: ask this job's next question (or continue the application).

        ``chat_id`` None (no Telegram): the questions wait on the dashboard, which answers them with
        ``answer`` and ``continue_after_answers``.
        """
        await self._ask_next(chat_id, task.key)

    def _continue(self, job_id: str) -> bool:
        """No question is left: the application goes on (True). False while questions wait."""
        if self.answers.pending(job_id):
            return False
        waiting = self.waiting()
        if waiting is None or waiting[0] == job_id:
            self.chats.set_state(ASKING_KEY, None)
        self.queue.enqueue(names.APPLY, job_id, priority=Priority.APPLY)
        return True

    # ---- answers from the dashboard ------------------------------------------------------------------

    def answer(self, job_id: str, key: str, text: str) -> str | None:
        """One answer typed or picked on the dashboard: saved as a chat answer is; the problem, or None.

        A question that is no longer waiting (answered in Telegram meanwhile) is left alone.
        """
        question = self._question(job_id, key)
        if question is None:
            return None
        answer, why = fitted(question, text)
        if answer is None:
            return why
        self._save(job_id, question, answer)
        return None

    def continue_after_answers(self, job_id: str) -> bool:
        """After answers on the dashboard: True when none are left and the application continues.

        While some are left and Telegram was asking one of them, Telegram asks the next one.
        """
        if self._continue(job_id):
            return True
        waiting = self.waiting()
        if waiting is not None and waiting[0] == job_id and self._question(job_id, waiting[1]) is None:
            self.queue.enqueue(names.ASK_QUESTIONS, job_id, priority=Priority.ALERT)
        return False

    def decline(self, job_id: str) -> None:
        """Don't apply to this job: its questions are dropped and the job is skipped."""
        self.answers.clear(job_id)
        waiting = self.waiting()
        if waiting is not None and waiting[0] == job_id:
            self.chats.set_state(ASKING_KEY, None)
        self.chats.set_job_status(job_id, JobStatus.SKIPPED)

    # ---- the chat ------------------------------------------------------------------------------------

    async def _ask_next(self, chat_id: int | None, job_id: str) -> None:
        if self._continue(job_id):
            if chat_id is not None:
                await self.api.send_message(
                    chat_id, "✅ Thanks, that's everything. Filling the application now…"
                )
            return
        if chat_id is None:
            return  # no chat: the questions wait on the dashboard
        pending = self.answers.pending(job_id)
        question = pending[0]
        job = self.jobs.get(job_id)
        company = escape(job.company) if job else "The employer"
        text = f"📝 <b>{company}</b> asks ({len(pending)} left):\n\n{escape(question.label)}"
        markup: Markup | None = None
        if question.options and len(question.options) <= MAX_BUTTONS and question.type != "checkbox":
            rows = [[(option[:60], f"ans:{i}")] for i, option in enumerate(question.options)]
            markup = buttons(*rows, [("⏭ Don't apply to this job", "ans:cancel")])
        else:
            hint = f"\nAnswers: {escape(', '.join(question.options))}" if question.options else ""
            keep = (
                "I'll use it for this application only."
                if company_specific(question.label)
                else "I'll remember it for other applications."
            )
            text += f"{hint}\n\n<i>Reply with your answer. {keep}</i>"
            markup = buttons([("⏭ Don't apply to this job", "ans:cancel")])
        self.chats.set_state(ASKING_KEY, json.dumps({"job": job_id, "key": question.key}))
        await self.api.send_message(chat_id, text, markup)

    async def on_text(self, chat_id: int, text: str) -> bool:
        """A typed answer to the waiting question. False when no question is waiting."""
        waiting = self.waiting()
        if waiting is None:
            return False
        job_id, key = waiting
        question = self._question(job_id, key)
        if question is None:
            self.chats.set_state(ASKING_KEY, None)
            return False
        answer, why = fitted(question, text)
        if answer is None:
            await self.api.send_message(chat_id, f"🤔 {why}\n\n{escape(question.label)}")
            return True
        await self._record(chat_id, job_id, question, answer)
        return True

    async def on_button(self, chat_id: int, data: str) -> None:
        """A tapped answer button (``ans:<n>``) or "don't apply" (``ans:cancel``)."""
        waiting = self.waiting()
        if waiting is None:
            return
        job_id, key = waiting
        _, _, choice = data.partition(":")
        if choice == "cancel":
            self.decline(job_id)
            await self.api.send_message(chat_id, "OK, I won't apply to that one.")
            return
        question = self._question(job_id, key)
        if question is None or not choice.isdigit() or int(choice) >= len(question.options):
            return  # an old button from an earlier question
        await self._record(chat_id, job_id, question, question.options[int(choice)])

    async def _record(self, chat_id: int, job_id: str, question: FormField, answer: str) -> None:
        self._save(job_id, question, answer)
        await self._ask_next(chat_id, job_id)

    def _save(self, job_id: str, question: FormField, answer: str) -> None:
        # An answer about this employer ("Why us?") is kept for this job only, never sent to another company.
        key = for_job(question.label, job_id) if company_specific(question.label) else question.label
        self.answers.save(key, answer)
        self.answers.answered(job_id, question.key)
