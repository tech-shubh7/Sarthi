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
    auto_daily_limit: int


@dataclass(frozen=True, slots=True)
class Pending:
    """A filled form waiting for the user's Submit."""

    job_id: str
    screenshot: str  # path under the data folder, "" when none was taken


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

    def pending(self) -> dict[str, Pending]:
        """Filled forms waiting for Submit, by job id."""
        ...

    def submit(self, job_id: str) -> bool:
        """Send the filled form (the user approved it on the dashboard). False when nothing is waiting."""
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
