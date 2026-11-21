"""The chat message that comes with a job's tailored documents: keyword fit and anything to check."""

from __future__ import annotations

from html import escape
from typing import Any

MAX_LISTED = 8


def _names(items: list[Any]) -> str:
    shown = [escape(str(i)) for i in items[:MAX_LISTED]]
    more = len(items) - MAX_LISTED
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def report_text(report: dict[str, Any]) -> str:
    """Telegram HTML summarising a documents report."""
    keywords = report.get("keywords") or {}
    lines = [
        f"📄 <b>Tailored for {escape(str(report.get('role', 'this job')))}</b>"
        f" at {escape(str(report.get('company', '')))}",
    ]
    on, have, missing = (list(keywords.get(k) or []) for k in ("on_resume", "have_not_shown", "missing"))
    if on or have or missing:
        lines.append(f"Job keywords on your resume: <b>{keywords.get('score', 0)}%</b>")
    else:
        lines.append("The posting names no specific skills, so there is no keyword check.")
    if on:
        lines.append(f"✓ On it: {_names(on)}")
    if have:
        lines.append(f"➕ You have these but the resume doesn't say them, add them if true: {_names(have)}")
    if missing:
        lines.append(f"✗ The job asks for, not in your profile: {_names(missing)}")
    if not report.get("tailored_by_ai"):
        lines.append("ℹ️ No AI answer, so this is your original resume, not tailored.")
    if report.get("kept_original"):
        lines.append(
            f"ℹ️ Kept your original wording in {len(report['kept_original'])} place(s) where the AI "
            "added something your resume doesn't say."
        )
    if not report.get("letter_by_ai"):
        lines.append("ℹ️ The cover letter is a short plain one built from your resume.")
    for problem in list(report.get("ats") or [])[:3]:
        lines.append(f"⚠️ {escape(str(problem))}")
    return "\n".join(lines)
