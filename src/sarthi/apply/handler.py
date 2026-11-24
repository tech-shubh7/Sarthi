"""The ``apply`` and ``submit`` tasks: fill a form, wait for the user's approval, submit, report.

Rule-based filling (``engine.py``) works on Greenhouse, Lever, Ashby and plain forms. Anything that needs a
person is handed over with the link, never forced: a CAPTCHA, a Workday account, a job board that needs a
login (LinkedIn, Indeed, Naukri, ...), a form that did not load, or required fields that could not be entered.
Guards: one application per role at a company, and a daily limit per site.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sarthi.apply import adapters
from sarthi.apply.adapters import FillError
from sarthi.apply.agent import LoginWallError
from sarthi.apply.engine import ApplyEngine, SubmitResult
from sarthi.apply.linkedin import LimitReachedError, LoginNeededError
from sarthi.clock import Clock, utcnow
from sarthi.db.answers import AnswerStore
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.chat import ChatStore
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.profile.applicant import form_context
from sarthi.profile.models import Profile
from sarthi.queue import names
from sarthi.queue.tasks import DeferError, Priority, TaskQueue

log = logging.getLogger(__name__)

WAIT_FOR_PROFILE_S = 3600
# Boards where applying needs the user's own account and Sarthi has no adapter yet. LinkedIn (Easy Apply) and
# Naukri are filled with the login the user saves once with `sarthi login`.
LOGIN_BOARDS = (
    "indeed.", "glassdoor.", "ziprecruiter.", "seek.com", "bayt.com", "foundit.", "monster.", "instahyre.",
    "wellfound.com", "hirist.", "shine.com",
)  # fmt: skip
LIMIT_WAIT_S = 6 * 3600
DAY = timedelta(days=1)


def site_of(url: str) -> str:
    """The form's host, e.g. ``jobs.lever.co``."""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def needs_login(url: str) -> bool:
    """A job board where applying needs the user's own logged-in account."""
    host = site_of(url)
    return any(board in host for board in LOGIN_BOARDS)


def login_help(error: LoginWallError) -> str:
    """What to tell the user when a site wants a sign-in or a new account before the application."""
    if error.account:
        return (
            f"{error}. Sarthi does not create accounts or keep passwords: create it once on the site, log in "
            "with /login, then tap Apply again (or apply there yourself)."
        )
    return (
        f"{error}. Log in once with /login (it keeps the site's session, never your password), then tap "
        "Apply again."
    )


class Applier:
    """Handles ``apply`` and ``submit`` tasks."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        engine: ApplyEngine,
        jobs: JobStore,
        documents: DocumentStore,
        answers: AnswerStore,
        applications: ApplicationStore,
        *,
        queue: TaskQueue,
        load_profile: Callable[[], Profile | None],
        data_dir: Path,
        daily_limit: int = 30,
        chats: ChatStore | None = None,
        clock: Clock = utcnow,
    ) -> None:
        """Fill forms with ``engine``; record outcomes in ``applications``; tell the chat via ``queue``.

        ``chats`` says which jobs are marked for auto-apply.
        """
        self.engine = engine
        self.jobs = jobs
        self.documents = documents
        self.answers = answers
        self.applications = applications
        self.queue = queue
        self._load_profile = load_profile
        self.data_dir = data_dir
        self.daily_limit = daily_limit
        self.chats = chats
        self._clock = clock

    def _result(self, job_id: str, outcome: str, detail: str, **extra: Any) -> None:
        self.queue.enqueue(
            names.APPLY_RESULT,
            job_id,
            {"outcome": outcome, "detail": detail, **extra},
            priority=Priority.ALERT,
        )

    def _hand_over(self, job_id: str, url: str, why: str) -> None:
        self.applications.record(job_id, ApplicationStatus.NEEDS_HUMAN, site=site_of(url), detail=why)
        self._result(job_id, "needs_human", why, url=url)

    def _relative(self, path: Path | None) -> str:
        if path is None:
            return ""
        try:
            return str(path.relative_to(self.data_dir))
        except ValueError:
            return str(path)

    async def handle_apply(self, task: TaskRecord) -> None:  # noqa: PLR0911, PLR0912  (each way an application can stop)
        """Fill the job's form and ask the user to approve it (or hand it over)."""
        profile = self._load_profile()
        if profile is None:
            raise DeferError(WAIT_FOR_PROFILE_S)
        job = self.jobs.get(task.key)
        ctx = form_context(
            task.key,
            profile,
            jobs=self.jobs,
            documents=self.documents,
            answers=self.answers,
            data_dir=self.data_dir,
        )
        if job is None or ctx is None:
            return
        url = str(job.apply_url or job.url)
        if self.documents.get(task.key) is None:  # the tailored resume and letter come first
            self.queue.enqueue(names.DOCUMENTS, task.key, {"apply": True}, priority=Priority.ALERT)
            return
        if (key := self.jobs.role_key_of(task.key)) and self.applications.sent_for_role(key, task.key):
            self._result(task.key, "duplicate", f"You already applied to {job.title} at {job.company}.")
            return
        if adapters.is_workday(url):
            self._hand_over(task.key, url, "Workday forms need your own Workday account for this employer.")
            return
        board = needs_login(url)
        if board and not self.engine.walks:
            self._hand_over(task.key, url, f"{site_of(url)} needs your own logged-in account to apply.")
            return
        since = self._clock() - DAY
        if self.applications.sent_to_site_since(site_of(url), since) >= self.daily_limit:
            raise DeferError(3600)  # the site's daily limit: try again in an hour
        try:
            result = await self.engine.fill(task.key, url, ctx, one_click=board)
        except LimitReachedError:
            raise DeferError(LIMIT_WAIT_S) from None  # the site's own daily limit: later, not by hand
        except LoginNeededError as error:
            self._hand_over(task.key, url, str(error))
            return
        except LoginWallError as error:
            self._hand_over(task.key, url, login_help(error))
            return
        except FillError as error:
            self._hand_over(task.key, url, f"The form could not be filled: {error}")
            return
        auto = self.chats is not None and self.chats.is_auto(task.key)
        if auto and self.chats is not None:
            self.chats.set_auto(task.key, on=False)  # one try: whatever happens next, the user decides
        if result.blanks:
            self.answers.set_pending(
                task.key, result.blanks
            )  # the form asked more than its public data showed
            await self.engine.close_job(task.key)
            self.queue.enqueue(names.ASK_QUESTIONS, task.key, priority=Priority.ALERT)
            return
        if result.captcha:
            await self.engine.close_job(task.key)
            self._hand_over(task.key, url, "The form has a CAPTCHA, which only you can solve.")
            return
        if result.failed:
            await self.engine.close_job(task.key)
            fields = "; ".join(f"{label} ({why})" for label, why in result.failed[:5])
            self._hand_over(task.key, url, f"Some required fields could not be entered: {fields}")
            return
        self.applications.record(task.key, ApplicationStatus.FILLED, site=site_of(url))
        if auto and not board:
            # Every answer came from the profile, a saved answer or the posting (questions only the user can
            # answer stop above), and this is not a job board: sent without a tap, as the user chose.
            self.queue.enqueue(
                names.SUBMIT, task.key, {"code": result.code, "auto": True}, priority=Priority.ALERT
            )
            return
        self.queue.enqueue(
            names.APPLY_PREVIEW,
            task.key,
            {
                "code": result.code,
                "screenshot": self._relative(result.screenshot),
                "filled": result.filled[:30],
                "system": result.adapter,
            },
            priority=Priority.ALERT,
        )

    async def finish_by_hand(self, job_id: str, engine: ApplyEngine) -> SubmitResult | None:
        """Fill the form in ``engine`` (a browser on the user's screen) and wait for their own Submit.

        Sarthi only fills and watches: the user solves the CAPTCHA and presses Submit. Returns how it ended,
        recorded and sent to the chat, or None when the job or the profile is no longer stored.
        """
        profile = self._load_profile()
        job = self.jobs.get(job_id)
        ctx = (
            form_context(
                job_id,
                profile,
                jobs=self.jobs,
                documents=self.documents,
                answers=self.answers,
                data_dir=self.data_dir,
            )
            if job is not None and profile is not None
            else None
        )
        if job is None or ctx is None:
            return None
        url = str(job.apply_url or job.url)
        await engine.fill(
            job_id, url, ctx, one_click=needs_login(url)
        )  # blanks: the user fills them there too
        outcome = await engine.watch(job_id)
        status = (
            ApplicationStatus.SUBMITTED if outcome.outcome == "submitted" else ApplicationStatus.NEEDS_HUMAN
        )
        detail = (
            outcome.detail if outcome.outcome == "submitted" else f"CAPTCHA form not sent: {outcome.detail}"
        )
        shot = self._relative(outcome.screenshot)
        self.applications.record(job_id, status, site=site_of(url), detail=detail, screenshot=shot)
        self._result(job_id, outcome.outcome, detail, url=url, screenshot=shot)
        return outcome

    async def handle_submit(self, task: TaskRecord) -> None:
        """Submit the fill the user approved (the task carries its one-time code)."""
        job = self.jobs.get(task.key)
        if job is None:
            return
        url = str(job.apply_url or job.url)
        outcome = await self.engine.submit(task.key, str(task.payload.get("code") or ""))
        shot = self._relative(outcome.screenshot)
        if outcome.outcome == "expired":
            self._result(task.key, "expired", outcome.detail)
            self.queue.enqueue(
                names.APPLY, task.key, priority=Priority.NORMAL
            )  # fill again for a fresh approval
            return
        status = {
            "submitted": ApplicationStatus.SUBMITTED,
            "needs_human": ApplicationStatus.NEEDS_HUMAN,
        }.get(outcome.outcome, ApplicationStatus.FAILED)
        detail = outcome.detail
        if task.payload.get("auto") and outcome.outcome == "submitted":
            detail = f"🤖 Sent by auto-apply. {detail}"
        self.applications.record(task.key, status, site=site_of(url), detail=detail, screenshot=shot)
        self._result(task.key, outcome.outcome, detail, url=url, screenshot=shot)
