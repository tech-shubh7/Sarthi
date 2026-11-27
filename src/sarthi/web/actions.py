"""What the dashboard may change, as a contract the running app fulfils.

The web layer never writes to the database itself: ``sarthi run`` hands it an object with these methods (the
same steps the Telegram buttons take). ``sarthi dashboard`` on its own has none, so its pages only read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sarthi.profile.models import Profile


@dataclass(frozen=True, slots=True)
class Controls:
    """The switches shown on the settings page."""

    paused: bool
    auto_line: int | None  # the match % from which Sarthi applies by itself; None: off
    auto_wait_minutes: int
    auto_daily_limit: int | None  # None: no daily cap
    auto_boards: bool = False  # auto-apply also sends on job boards with the user's login
    board_automation: bool = True


@dataclass(frozen=True, slots=True)
class MatchBatch:
    """Where the eligible matches were routed."""

    scheduled: int = 0
    manual: int = 0


@dataclass(frozen=True, slots=True)
class Pending:
    """A filled form waiting for the user's Submit."""

    job_id: str
    screenshot: str  # path under the data folder, "" when none was taken


@dataclass(frozen=True, slots=True)
class MailSettings:
    """Where reply tracking reads mail. The password is never part of it: only whether one is saved."""

    host: str
    port: int
    user: str
    folder: str
    enabled: bool
    password_saved: bool = False
    checked_at: str = ""  # the last check (ISO), "" before the first
    check_ok: bool = True
    check_text: str = ""  # its outcome in plain words


class DashboardActions(Protocol):
    """The changes the dashboard can make (each mirrors a Telegram command or button)."""

    def profile(self) -> Profile | None:
        """The saved profile, or None before the first setup."""
        ...

    def save_profile(self, profile: Profile) -> int:
        """Save it and re-match recent jobs; returns how many will be matched again."""
        ...

    def resume_text(self, data: bytes) -> str:
        """The text of an uploaded resume (PDF, .docx or text); raises ``ValueError`` with the reason."""
        ...

    def controls(self) -> Controls:
        """The current switches."""
        ...

    def set_paused(self, paused: bool) -> None:
        """Pause or resume instant alerts (/pause, /resume)."""
        ...

    def set_auto_line(self, line: str) -> str | None:
        """``"off"`` or a percent (75-100); returns the problem, or None when saved (/autoapply)."""
        ...

    def choose(self, job_id: str, action: str) -> bool:
        """Apply, skip or later on one job, like its Telegram buttons. False for an unknown job."""
        ...

    def queue_matches(self) -> MatchBatch:
        """Schedule eligible current-profile matches using the existing auto-apply threshold."""
        ...

    def pending(self) -> dict[str, Pending]:
        """Filled forms waiting for Submit, by job id."""
        ...

    def submit(self, job_id: str) -> bool:
        """Send the filled form (the user approved it on the dashboard). False when nothing is waiting."""
        ...

    def finish(self, job_id: str) -> bool:
        """Open the filled form on this computer to finish by hand (a CAPTCHA). False when it cannot start."""
        ...

    def refill(self, job_id: str) -> bool:
        """Fill a form again whose filled page is gone (Sarthi restarted). False for an unknown job."""
        ...

    def mark_applied(self, job_id: str) -> bool:
        """The user applied on the site themselves ("I applied"). False for an unknown job."""
        ...

    def cancel(self, job_id: str) -> bool:
        """Drop the filled form; nothing is sent. False when nothing is waiting."""
        ...

    # ---- without Telegram: questions, auto-apply's veto, site logins ----

    def answer_questions(self, job_id: str, answers: dict[str, str]) -> dict[str, str]:
        """Answer a form's questions (field key -> answer), as in Telegram; problems by field key.

        Blank answers are left for later. When none are left the application continues.
        """
        ...

    def decline_questions(self, job_id: str) -> bool:
        """Don't apply to this job (its questions are dropped). False for an unknown job."""
        ...

    def cancel_auto(self, job_id: str) -> bool:
        """Cancel an auto-apply in its veto window. False for an unknown job."""
        ...

    def apply_now(self, job_id: str) -> bool:
        """Apply now instead of waiting out the veto window. False when no auto-apply waits."""
        ...

    def login_sites(self) -> list[tuple[str, str]]:
        """The job sites one can log in to for applying: (key, name)."""
        ...

    def site_login(self, site: str) -> bool:
        """Open a site's login page on this computer. False when another browser window is open."""
        ...

    def screen_link(self) -> str | None:
        """While a login or a form to finish is open on a screen in the browser (Docker): its link."""
        ...

    # ---- mail (reply tracking) ----

    def mail(self) -> MailSettings:
        """The mailbox setup in force (the dashboard's values over ``.env``), without its password."""
        ...

    def save_mail(self, settings: MailSettings, password: str) -> None:
        """Keep the mailbox setup; an empty ``password`` keeps the saved one. Counts from the next check."""
        ...

    async def test_mail(self, settings: MailSettings, password: str) -> str:
        """Log in read-only and say what happened in plain words (nothing is saved)."""
        ...
