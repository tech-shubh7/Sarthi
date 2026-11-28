"""The ``apply`` and ``submit`` tasks: fill a form, wait for the user's approval, submit, report.

Rule-based filling (``engine.py``) works on Greenhouse, Lever, Ashby and plain forms. Anything that needs a
person is handed over with the link, never forced: a CAPTCHA, a Workday account, a job board that needs a
login (LinkedIn, Indeed, Naukri, ...), a form that did not load, or required fields that could not be entered.
A LinkedIn or Naukri job that applies on the company's own site continues there (when that address is a public
web page), with the same guards and approval. Guards: one application per role at a company, and a daily
limit per site (the site the form is on).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sarthi.apply import adapters, linkedin, naukri, redirects
from sarthi.apply.adapters import FillError, PostingClosedError
from sarthi.apply.agent import LoginWallError
from sarthi.apply.engine import (
    UNKNOWN,
    ApplyEngine,
    EnginePausedError,
    FillResult,
    SubmitResult,
)
from sarthi.apply.external import ExternalApplyError
from sarthi.apply.linkedin import LimitReachedError, LoginNeededError
from sarthi.clock import Clock, utcnow
from sarthi.db.answers import AnswerStore
from sarthi.db.applications import (
    SENT,
    UNCONFIRMED_PREFIX,
    ApplicationRecord,
    ApplicationStatus,
    ApplicationStore,
)
from sarthi.db.chat import ChatStore, JobStatus
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.jobs.forms import FormContext
from sarthi.jobs.net import is_public_url
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
    "wellfound.com", "hirist.", "shine.com", "jobleads.com", "jobgether.com",
)  # fmt: skip
BOARD_MANUAL = (
    "Open this job on the site and submit it yourself. Browser automation is disabled for job boards."
)
LIMIT_WAIT_S = 6 * 3600
PAUSED_WAIT_S = 60  # a browser window (a login, a form finished by hand) holds the profile
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


def is_job_board(url: str) -> bool:
    """Whether the form belongs to a job board with its own login."""
    return needs_login(url) or linkedin.is_linkedin(url) or naukri.is_naukri(url)


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


async def external_refusal(url: str) -> str:
    """Why a board's company-site address is not opened ("" when it may be): it must be a public web page."""
    if not await is_public_url(url):
        return f"its link ({site_of(url) or url[:80]}) is not a public web address, so Sarthi did not open it"
    if linkedin.is_linkedin(url) or naukri.is_naukri(url):
        return "its link leads back to a job board"
    return ""


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
        auto_on_boards: Callable[[], bool] = lambda: False,
        allow_board_automation: bool = True,
        resolve_link: Callable[[str], Awaitable[str | None]] = redirects.resolve,
    ) -> None:
        """Fill forms with ``engine``; record outcomes in ``applications``; tell the chat via ``queue``.

        ``chats`` says which jobs are marked for auto-apply; ``auto_on_boards`` whether auto-apply also sends
        on job boards with the user's login (the user's own choice, read at each application).
        """
        self.auto_on_boards = auto_on_boards
        self.allow_board_automation = allow_board_automation
        self.engine = engine
        self.jobs = jobs
        self.documents = documents
        self.answers = answers
        self.applications = applications
        self.queue = queue
        self._load_profile = load_profile
        self.data_dir = data_dir
        self.daily_limit = daily_limit
        self._submit_lock = asyncio.Lock()
        self.chats = chats
        self._clock = clock
        self._resolve_link = resolve_link

    async def resolve_target(self, job_id: str) -> None:
        """Replace an apply link that only redirects (Freehire's) with the employer's page it leads to.

        Done before documents are written and before any fill, so every guard (firewall, Workday, job board,
        the site's daily limit) judges the employer's site, not the redirector that refuses browsers.
        """
        job = self.jobs.get(job_id)
        if job is None:
            return
        url = str(job.apply_url or job.url)
        if not redirects.is_redirect_link(url):
            return
        if (real := await self._resolve_link(url)) is not None:
            try:
                self.jobs.set_apply_url(job_id, real)
            except ValueError:
                log.warning("%s: %s redirects to an unusable address; kept as it was", job_id, site_of(url))
                return
            log.info("%s: %s leads to %s", job_id, site_of(url), site_of(real))

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

    def _already_attempted(
        self, previous: ApplicationRecord | None, job_id: str, label: str, url: str
    ) -> bool:
        """Stop an application that went out or might already have gone out."""
        if previous is None:
            return False
        if previous.status in SENT:
            self._result(job_id, "duplicate", f"You already applied to {label}.")
            return True
        if previous.unconfirmed:
            self._result(job_id, UNKNOWN, previous.detail, url=url)
            return True
        return False

    def mark_manual_board(self, job_id: str, url: str) -> None:
        """Keep a job-board match in the application's manual list."""
        self.applications.record(
            job_id, ApplicationStatus.NEEDS_HUMAN, site=site_of(url), detail=BOARD_MANUAL
        )

    def skip_application_documents(self, job_id: str) -> bool:
        """Avoid writing job-specific documents for a job already sent or a manual-only board."""
        job = self.jobs.get(job_id)
        if job is None:
            return False
        url = str(job.apply_url or job.url)
        if self._already_attempted(
            self.applications.get(job_id), job_id, f"{job.title} at {job.company}", url
        ):
            return True
        if not self.allow_board_automation and is_job_board(url):
            self._hand_over(job_id, url, BOARD_MANUAL)
            return True
        if self.applications.firewall_blocked_since(site_of(url), self._clock() - DAY):
            # The site's firewall refused Sarthi's browser in the last day: no resume and letter are written
            # (paid AI) for a form that cannot be reached; the job goes straight to "apply yourself".
            self._hand_over(job_id, url, adapters.blocked_reason(url))
            return True
        return False

    async def handle_apply(self, task: TaskRecord) -> None:  # noqa: PLR0911  (each way an application can stop)
        """Fill the job's form and ask the user to approve it (or hand it over)."""
        profile = self._load_profile()
        if profile is None:
            raise DeferError(WAIT_FOR_PROFILE_S)
        await self.resolve_target(task.key)
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
        previous = self.applications.get(task.key)
        if self._already_attempted(previous, task.key, f"{job.title} at {job.company}", url):
            return
        made = self.documents.get(task.key)
        if made is None or made.profile_version != profile.version:
            # The tailored resume and letter come first, made from the current profile and resume: one made
            # from an older upload (another design, other links) is never sent.
            self.queue.enqueue(names.DOCUMENTS, task.key, {"apply": True}, priority=Priority.ALERT)
            return
        if (key := self.jobs.role_key_of(task.key)) and self.applications.sent_for_role(key, task.key):
            self._result(task.key, "duplicate", f"You already applied to {job.title} at {job.company}.")
            return
        filled = await self._fill(task.key, url, ctx)
        if filled is None:
            return
        result, url, board = filled  # url: where the form is (a board job's company site, when it went on)
        auto = self.chats is not None and self.chats.is_auto(task.key)
        auto_refill = task.payload.get("auto_refill") is True and not board
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
        if task.payload.get("approved"):
            # The user tapped Submit on an earlier fill of this form that closed before it was sent: their
            # approval carries over to this fill, which has no blanks, no failed field and no CAPTCHA.
            self.queue.enqueue(
                names.SUBMIT, task.key, {"code": result.code, "approved": True}, priority=Priority.APPLY
            )
            return
        if auto_refill or (auto and (not board or self.auto_on_boards())):
            # Every answer came from the profile, a saved answer or the posting (questions only the user can
            # answer stop above); a job board only when the user included them: sent without a tap.
            submission = {"code": result.code, "auto": True, **({"auto_refill": True} if auto_refill else {})}
            self.queue.enqueue(names.SUBMIT, task.key, submission, priority=Priority.APPLY)
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
            priority=Priority.APPLY,
        )

    def _manual_reason(self, url: str) -> str:
        if not self.allow_board_automation and is_job_board(url):
            return BOARD_MANUAL
        if adapters.is_workday(url):
            return "Workday forms need your own Workday account for this employer."
        return ""

    async def _fill(  # noqa: PLR0911  (each way a fill can stop)
        self, job_id: str, url: str, ctx: FormContext
    ) -> tuple[FillResult, str, bool] | None:
        """Fill the form at ``url`` with the guards; None when the job was handed over instead.

        A board job that applies on the company's own site goes on to that site once, when its address is a
        public web page: the same guards (Workday, logins, the site's daily limit) then apply to that site.
        Returns the fill, the address it was made at and whether that is a job board (its "Apply" may send).
        """
        for hop in range(2):
            if manual := self._manual_reason(url):
                self._hand_over(job_id, url, manual)
                return None
            board = needs_login(url)
            if board and not self.engine.walks:
                self._hand_over(job_id, url, f"{site_of(url)} needs your own logged-in account to apply.")
                return None
            if self.applications.sent_to_site_since(site_of(url), self._clock() - DAY) >= self.daily_limit:
                raise DeferError(3600)  # the site's daily limit: try again in an hour
            try:
                return await self.engine.fill(job_id, url, ctx, one_click=board), url, is_job_board(url)
            except ExternalApplyError as error:
                why = await external_refusal(error.url) if hop == 0 else "it led to yet another job board"
                if why:
                    self._hand_over(job_id, url, f"This job applies on the company's own site, but {why}.")
                    return None
                log.info("%s applies on the company's site: going on to %s", job_id, site_of(error.url))
                url = error.url
            except LimitReachedError:
                raise DeferError(LIMIT_WAIT_S) from None  # the site's own daily limit: later, not by hand
            except LoginNeededError as error:
                self._hand_over(job_id, url, str(error))
                return None
            except LoginWallError as error:
                self._hand_over(job_id, url, login_help(error))
                return None
            except EnginePausedError:
                raise DeferError(PAUSED_WAIT_S) from None
            except PostingClosedError as error:  # recorded as closed, not handed to the user
                detail = f"Not applied: {error}."
                self.applications.record(job_id, ApplicationStatus.FAILED, site=site_of(url), detail=detail)
                self._result(job_id, "closed", detail, url=url)
                return None
            except FillError as error:
                self._hand_over(job_id, url, f"The form could not be filled: {error}")
                return None
        return None  # not reached: the second hop always returns

    async def finish_by_hand(self, job_id: str, engine: ApplyEngine) -> SubmitResult | None:
        """Fill the form in ``engine`` (a browser on the user's screen) and wait for their own Submit.

        Sarthi only fills and watches: the user solves the CAPTCHA and presses Submit. Returns how it ended,
        recorded and sent to the chat, or None when the job or the profile is no longer stored.
        """
        profile = self._load_profile()
        await self.resolve_target(job_id)
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
        previous = self.applications.get(job_id)
        if previous is not None and self._already_attempted(
            previous, job_id, f"{job.title} at {job.company}", url
        ):
            return SubmitResult(UNKNOWN if previous.unconfirmed else "duplicate", previous.detail)
        if not self.allow_board_automation and is_job_board(url):
            self._hand_over(job_id, url, BOARD_MANUAL)
            return SubmitResult("needs_human", BOARD_MANUAL)
        try:
            await engine.fill(job_id, url, ctx, one_click=needs_login(url))  # blanks: the user fills them
        except ExternalApplyError as error:
            if why := await external_refusal(error.url):
                raise FillError(f"this job applies on the company's own site, but {why}") from error
            url = error.url
            if not self.allow_board_automation and is_job_board(url):
                self._hand_over(job_id, url, BOARD_MANUAL)
                return SubmitResult("needs_human", BOARD_MANUAL)
            await engine.fill(job_id, url, ctx, one_click=needs_login(url))
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
        """Submit one approved form at a time so dedupe and per-site limits see the last outcome."""
        async with self._submit_lock:
            choice = self.chats.job_status(task.key) if self.chats is not None else None
            if choice in (JobStatus.SKIPPED, JobStatus.LATER):
                await self.engine.close_job(task.key)
                return
            await self._submit(task)

    def _hold_interrupted_submit(self, job_id: str, site: str, url: str) -> bool:
        if not self.applications.press_unresolved(job_id):
            return False
        detail = UNCONFIRMED_PREFIX + "Sarthi stopped during Submit. Check the employer site before retrying."
        self.applications.record(
            job_id, ApplicationStatus.NEEDS_HUMAN, site=site, detail=detail, resolve_press=True
        )
        self._result(job_id, UNKNOWN, detail, url=url)
        return True

    async def _submit(self, task: TaskRecord) -> None:
        job = self.jobs.get(task.key)
        if job is None:
            return
        url = str(job.apply_url or job.url)
        filled = self.applications.get(task.key)
        site = filled.site if filled is not None and filled.site else site_of(url)  # the form's site, filled
        if self._already_attempted(filled, task.key, f"{job.title} at {job.company}", url) or (
            self._hold_interrupted_submit(task.key, site, url)
        ):
            return
        if is_job_board(f"https://{site}"):
            if not self.allow_board_automation:
                await self.engine.close_job(task.key)
                self._hand_over(task.key, url, BOARD_MANUAL)
                return
            if task.payload.get("auto") and not self.auto_on_boards():
                await self.engine.close_job(task.key)
                self.queue.enqueue(names.APPLY, task.key, priority=Priority.APPLY)
                return
        if (key := self.jobs.role_key_of(task.key)) and self.applications.sent_for_role(key, task.key):
            await self.engine.close_job(task.key)
            self._result(task.key, "duplicate", f"You already applied to {job.title} at {job.company}.")
            return
        if self.applications.sent_to_site_since(site, self._clock() - DAY) >= self.daily_limit:
            raise DeferError(3600)
        try:
            outcome = await self.engine.submit(
                task.key,
                str(task.payload.get("code") or ""),
                before_click=lambda: self.applications.mark_press_started(task.key),
            )
        except EnginePausedError:
            raise DeferError(PAUSED_WAIT_S) from None
        shot = self._relative(outcome.screenshot)
        if outcome.outcome == "expired":
            # Expired means no click. Carry each authorization once and run all form checks again.
            auto = task.payload.get("auto") is True
            carried = not auto and not task.payload.get("approved")
            auto_refill = (
                auto
                and filled is not None
                and filled.status is ApplicationStatus.FILLED
                and not task.payload.get("auto_refill")
                and not is_job_board(f"https://{site}")
            )
            self._result(task.key, "expired", outcome.detail, approved=carried, auto_refill=auto_refill)
            self.queue.enqueue(
                names.APPLY,
                task.key,
                {"approved": carried, **({"auto_refill": True} if auto_refill else {})},
                priority=Priority.APPLY,
            )
            return
        status = {
            "submitted": ApplicationStatus.SUBMITTED,
            "needs_human": ApplicationStatus.NEEDS_HUMAN,
            UNKNOWN: ApplicationStatus.NEEDS_HUMAN,  # pressed, perhaps sent: the user checks, no retry
        }.get(outcome.outcome, ApplicationStatus.FAILED)
        detail = UNCONFIRMED_PREFIX + outcome.detail if outcome.outcome == UNKNOWN else outcome.detail
        if task.payload.get("auto") and outcome.outcome == "submitted":
            detail = f"🤖 Sent by auto-apply. {detail}"
        self.applications.record(
            task.key, status, site=site, detail=detail, screenshot=shot, resolve_press=True
        )
        self._result(task.key, outcome.outcome, detail, url=url, screenshot=shot)
