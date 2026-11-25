"""The ``form_check`` task: before a browser opens, find the questions only the user can answer.

The form's real questions are read from the hiring system's public data (``questions.py``); each is matched
with the user's profile, resume, cover letter and earlier answers (``sarthi.jobs.forms``). Required questions
with no safe answer are stored and asked on Telegram; when none are left, the application continues. Forms
whose questions cannot be read this way go straight to the browser, which reads them itself.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import httpx

from sarthi.answers.questions import posting_for, read_questions
from sarthi.db.answers import AnswerStore
from sarthi.db.chat import ChatStore
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.jobs.forms import FormContext, unanswered
from sarthi.profile.applicant import form_context
from sarthi.profile.models import Profile
from sarthi.queue import names
from sarthi.queue.tasks import DeferError, Priority, TaskQueue

log = logging.getLogger(__name__)

WAIT_FOR_PROFILE_S = 3600


class FormChecker:
    """Handles ``form_check`` tasks."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        jobs: JobStore,
        documents: DocumentStore,
        answers: AnswerStore,
        queue: TaskQueue,
        load_profile: Callable[[], Profile | None],
        *,
        data_dir: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        chats: ChatStore | None = None,
    ) -> None:
        """Read jobs, documents and the answer bank; queue the next step on ``queue``."""
        self.jobs = jobs
        self.documents = documents
        self.answers = answers
        self.queue = queue
        self._load_profile = load_profile
        self.data_dir = data_dir
        self._transport = transport
        self.chats = chats

    def context(self, job_id: str, profile: Profile) -> FormContext | None:
        """What this job's form may be filled with, or None when the job is gone."""
        return form_context(
            job_id,
            profile,
            jobs=self.jobs,
            documents=self.documents,
            answers=self.answers,
            data_dir=self.data_dir,
        )

    async def handle(self, task: TaskRecord) -> None:
        """Ask the user what the form needs, or let the application continue."""
        profile = self._load_profile()
        if profile is None:
            raise DeferError(WAIT_FOR_PROFILE_S)
        job = self.jobs.get(task.key)
        ctx = self.context(task.key, profile)
        if job is None or ctx is None:
            return
        posting = posting_for(str(job.apply_url or ""), str(job.url))
        fields = await read_questions(posting, self._transport) if posting else None
        missing = unanswered(fields, ctx) if fields else []
        if missing:
            if self.chats is not None:
                self.chats.set_auto(task.key, on=False)  # the user answers, so the user also approves
            self.answers.set_pending(task.key, missing)
            self.queue.enqueue(names.ASK_QUESTIONS, task.key, priority=Priority.ALERT)
            log.info("%s: %d question(s) for the user before applying", job.company, len(missing))
            return
        self.answers.clear(task.key)
        self.queue.enqueue(names.APPLY, task.key, priority=Priority.APPLY)
