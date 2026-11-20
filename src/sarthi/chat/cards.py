"""How a job looks in a chat message.

Every value that comes from a job site is HTML-escaped: a posting must not be able to inject formatting or
links into the user's messages.
"""

from __future__ import annotations

from html import escape

from sarthi.db.matches import MatchRecord
from sarthi.jobs.geo import country_name
from sarthi.jobs.models import JobPosting, RemoteType, Salary

MAX_TITLE_CHARS = 120
MAX_LINE_CHARS = 200
_REMOTE_LABEL = {RemoteType.REMOTE: "Remote", RemoteType.HYBRID: "Hybrid", RemoteType.ONSITE: "On-site"}


def display_company(name: str) -> str:
    """Company name for people: a bare lowercase slug such as ``openai`` becomes ``Openai``."""
    if name.islower() and " " not in name:
        return name.replace("-", " ").replace("_", " ").title()
    return name


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def place_text(job: JobPosting) -> str:
    """Where the job is, for people: its location, else its countries, plus remote/hybrid."""
    parts: list[str] = []
    if job.location_text:
        parts.append(job.location_text)
    elif job.country_codes:
        parts.append(", ".join(sorted(country_name(c) for c in job.country_codes)))
    if job.remote_type in _REMOTE_LABEL:
        parts.append(_REMOTE_LABEL[job.remote_type])
    return " · ".join(parts) or "Location not stated"


def salary_text(salary: Salary | None) -> str | None:
    """A pay range for people ("INR 12,00,000-18,00,000 / year"), or None when none is given."""
    if salary is None or (salary.minimum is None and salary.maximum is None):
        return None
    low, high = salary.minimum, salary.maximum
    amount = f"{low:,.0f}–{high:,.0f}" if low is not None and high is not None else f"{(low or high):,.0f}"
    return f"{salary.currency} {amount} / {salary.period.value}"


def job_card(job: JobPosting, match: MatchRecord | None) -> str:
    """The message text for one job (Telegram HTML)."""
    title = escape(_clip(job.title, MAX_TITLE_CHARS))
    lines = [f"<b>{title}</b>", f"{escape(display_company(job.company))} · {escape(place_text(job))}"]
    if salary := salary_text(job.salary):
        lines.append(escape(salary))
    if match is not None:
        lines.append(f"\nMatch: <b>{match.score}%</b>")
        lines += [f"✓ {escape(_clip(reason, MAX_LINE_CHARS))}" for reason in match.reasons]
        lines += [f"• {escape(_clip(note, MAX_LINE_CHARS))}" for note in match.notes]
    url = escape(str(job.apply_url or job.url), quote=True)
    lines.append(f'\n<a href="{url}">Open the job</a>')
    return "\n".join(lines)
