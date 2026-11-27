"""Auto-apply with a veto window (opt-in, off by default).

When a strong match is at or above the user's own line (``/autoapply 85``), Sarthi says so in the chat and
waits (5 minutes by default) with Cancel and Apply now buttons. If nobody cancels, the usual steps run:
tailored documents, the form's questions, the fill. The form is sent without a tap only when every answer came
from the profile, a saved answer or the posting, and the site is not a job board that bans bulk applying;
anything else comes back as the usual preview with Submit. Never below ``MIN_LINE``; no daily cap unless
the user sets ``daily_limit`` (each site's own daily limit still applies).
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from sarthi.chat.cards import display_company
from sarthi.chat.telegram.api import Markup, buttons
from sarthi.clock import Clock, utcnow
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchRecord
from sarthi.queue import names
from sarthi.queue.tasks import Priority, TaskQueue

MIN_LINE = 75  # below this the AI's evidence is too thin to send an application nobody looked at
LINE_KEY = "auto_apply.line"  # the user's own line ("85") or "off"; unset: the .env setting
COUNT_KEY = "auto_apply.count"  # "YYYY-MM-DD n": how many were scheduled today
BOARDS_KEY = "auto_apply.boards"  # "on" / "off": also send on job boards with the user's login; unset: .env
TAPS_KEY = "auto_apply.taps"  # "on" / "off": a tapped Apply sends a clean fill without a Submit tap
HELP = (
    "🤖 <b>Auto-apply</b> is {state}.\n\n"
    "When a job's match is at or above your line, I tell you and wait {wait} before applying, so you can "
    "cancel. A form is sent without your tap only when I could fill every answer from your profile; "
    "otherwise you get the usual preview. {boards}{limit}.\n\n"
    "/autoapply 85 sets your line (75-100), /autoapply off turns it off, "
    "/autoapply boards on (or off) includes LinkedIn, Naukri and other job boards, "
    "/autoapply taps on (or off) sends the jobs you tap Apply on without asking for Submit.\n\n{taps}"
)
TAPS_ON = "Apply taps: sent without a Submit tap when every answer came from your profile."
TAPS_OFF = "Apply taps: you check the filled form and tap Submit."
BOARDS_ON = (
    "Also on LinkedIn, Naukri and other job boards with your login (they may restrict accounts that apply "
    "in bulk; each site gets at most its daily limit)"
)
BOARDS_OFF = "On LinkedIn, Naukri and other job boards it waits for your Submit"


@dataclass(frozen=True, slots=True)
class AutoApplySettings:
    """From the .env file: the default line (None: off), a daily cap, the veto window, job boards."""

    line: int | None = None
    daily_limit: int | None = None
    wait_s: float = 300
    job_boards: bool = False  # also send on job boards that use the user's own login
    taps_send: bool = False  # a tapped Apply sends a clean fill without a Submit tap


class AutoApply:
    """Decides, schedules and cancels automatic applications."""

    def __init__(
        self,
        chats: ChatStore,
        jobs: JobStore,
        queue: TaskQueue,
        settings: AutoApplySettings,
        *,
        clock: Clock = utcnow,
    ) -> None:
        """Keep state in ``chats``; schedule on ``queue``."""
        self.chats = chats
        self.jobs = jobs
        self.queue = queue
        self.settings = settings
        self._clock = clock

    def line(self) -> int | None:
        """The match % from which jobs are auto-applied, or None when auto-apply is off."""
        own = self.chats.get_state(LINE_KEY)
        if own == "off":
            return None
        if own and own.isdigit():
            return max(MIN_LINE, min(100, int(own)))
        return self.settings.line

    def boards(self) -> bool:
        """True when auto-apply also sends on job boards with the user's login (LinkedIn, Naukri, ...)."""
        own = self.chats.get_state(BOARDS_KEY)
        return own == "on" if own in ("on", "off") else self.settings.job_boards

    def set_boards(self, *, on: bool) -> None:
        """Include (or leave out) job boards with the user's login."""
        self.chats.set_state(BOARDS_KEY, "on" if on else "off")

    def taps_send(self) -> bool:
        """True when a tapped Apply sends a clean fill by itself (no Submit tap)."""
        own = self.chats.get_state(TAPS_KEY)
        return own == "on" if own in ("on", "off") else self.settings.taps_send

    def set_taps(self, *, on: bool) -> None:
        """Let a tapped Apply send by itself, or ask for Submit again."""
        self.chats.set_state(TAPS_KEY, "on" if on else "off")

    def qualifies(self, match: MatchRecord | None) -> bool:
        """Check the score and evidence for an auto-apply, including jobs already scheduled."""
        line = self.line()
        return line is not None and match is not None and match.ai_used and match.score >= line

    def wanted(self, match: MatchRecord | None) -> bool:
        """Decide whether a new match can take one of today's auto-apply slots."""
        return self.qualifies(match) and (
            self.settings.daily_limit is None or self._today_count() < self.settings.daily_limit
        )

    def _today(self) -> str:
        return self._clock().date().isoformat()

    def _today_count(self) -> int:
        day, _, count = (self.chats.get_state(COUNT_KEY) or "").partition(" ")
        return int(count) if day == self._today() and count.isdigit() else 0

    def schedule(self, job_id: str, score: int, *, resume: bool = False) -> tuple[str, Markup]:
        """Mark the job for auto-apply, queue it after the veto window; the chat message and its buttons."""
        self.chats.set_auto(job_id, on=True)
        self.chats.set_state(COUNT_KEY, f"{self._today()} {self._today_count() + 1}")
        self.queue.enqueue(
            names.AUTO_APPLY,
            job_id,
            {"resume": True} if resume else None,
            delay=self.settings.wait_s,
            priority=Priority.NORMAL,
        )
        job = self.jobs.get(job_id)
        what = f"<b>{escape(job.title)}</b> at {escape(display_company(job.company))}" if job else "this job"
        text = (
            f"🤖 {score}% match: I'll apply to {what} in {_wait_text(self.settings.wait_s)} "
            "unless you cancel. I'll send it myself only if I can fill every answer from your profile."
        )
        return text, buttons([("❌ Cancel", f"auto:cancel:{job_id}"), ("🚀 Apply now", f"auto:now:{job_id}")])

    def cancel(self, job_id: str) -> None:
        """No automatic application for this job (the user can still tap Apply on its card)."""
        self.chats.set_auto(job_id, on=False)

    def change_line(self, argument: str) -> str | None:
        """Store ``off`` or a percent (MIN_LINE-100); the problem, or None when stored (or nothing given)."""
        word = argument.strip().lower().rstrip("%").strip()
        if word.startswith(("boards", "taps")):
            kind, _, choice = word.partition(" ")
            if choice.strip() not in ("on", "off"):
                return f"Say /autoapply {kind} on, or /autoapply {kind} off."
            (self.set_boards if kind == "boards" else self.set_taps)(on=choice.strip() == "on")
        elif word in ("off", "stop", "no", "0"):
            self.chats.set_state(LINE_KEY, "off")
        elif word.isdigit() and MIN_LINE <= int(word) <= 100:  # noqa: PLR2004
            self.chats.set_state(LINE_KEY, word)
        elif word:
            return f"Pick a line from {MIN_LINE} to 100, e.g. /autoapply 85, or off."
        return None

    def set_line(self, argument: str) -> str:
        """``/autoapply [off | <percent>]``: the reply to send."""
        if problem := self.change_line(argument):
            return problem
        line = self.line()
        state = f"on from <b>{line}%</b>" if line is not None else "<b>off</b>"
        cap = self.settings.daily_limit
        limit = f", at most {cap} a day" if cap is not None else ""
        boards = BOARDS_ON if self.boards() else BOARDS_OFF
        taps = TAPS_ON if self.taps_send() else TAPS_OFF
        return HELP.format(
            state=state, wait=_wait_text(self.settings.wait_s), boards=boards, limit=limit, taps=taps
        )


def _wait_text(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60 or minutes % 60:  # noqa: PLR2004
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = minutes // 60
    return f"{hours} hour{'' if hours == 1 else 's'}"
