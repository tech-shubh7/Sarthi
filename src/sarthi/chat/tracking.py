"""Messages about replies from employers and follow-up drafts."""

from __future__ import annotations

from html import escape
from typing import Any

from sarthi.jobs.models import JobPosting

HEADLINES = {
    "confirmation": "📨 Application received",
    "rejection": "📭 Not selected this time",
    "interview": "🎉 Interview invitation",
    "assessment": "📝 They sent a test or asked for more",
    "offer": "🏆 Offer",
    "other": "📬 Reply",
}
MAX_LINES = 5


def reply_text(job: JobPosting | None, payload: dict[str, Any]) -> str:
    """Telegram HTML for one employer reply."""
    kind = str(payload.get("kind") or "other")
    where = f"<b>{escape(job.title)}</b> at {escape(job.company)}" if job else "an application"
    lines = [f"{HEADLINES.get(kind, HEADLINES['other'])}: {where}"]
    if subject := str(payload.get("subject") or ""):
        lines.append(f"<i>{escape(subject[:200])}</i>")
    if summary := str(payload.get("summary") or ""):
        lines.append(escape(summary[:300]))
    for date in list(payload.get("dates") or [])[:MAX_LINES]:
        lines.append(f"🗓 {escape(str(date)[:200])}")
    for action in list(payload.get("actions") or [])[:MAX_LINES]:
        lines.append(f"👉 {escape(str(action)[:200])}")
    if kind == "rejection":
        lines.append("It happens to everyone. I'll keep finding better fits.")
    return "\n".join(lines)


def followup_text(job: JobPosting | None, payload: dict[str, Any]) -> str:
    """Telegram HTML for a follow-up draft."""
    where = f"<b>{escape(job.title)}</b> at {escape(job.company)}" if job else "an application"
    days = int(payload.get("days") or 0)
    draft = escape(str(payload.get("draft") or "")[:2000])
    return (
        f"⏰ No reply yet from {where} ({days} days).\n"
        "A short follow-up often helps. Here's a draft you can send to the recruiter (email or LinkedIn):\n\n"
        f"<pre>{draft}</pre>"
    )
