"""Messages about job sources that stopped (or started again) working."""

from __future__ import annotations

from html import escape
from typing import Any

_KNOWN = {
    "catalog:ats-scrapers": "the company-board snapshot (~192k companies)",
    "search:freehire": "Freehire",
    "boards:adzuna": "Adzuna",
    "boards:jooble": "Jooble",
    "feed:telegram": "your Telegram channels",
    "feed:hackernews": "Hacker News “Who is hiring”",
}
_KEY_SOURCES = ("boards:adzuna", "boards:jooble")


def source_label(name: str) -> str:
    """A readable name for a source id like ``greenhouse:razorpay`` or ``boards:linkedin+indeed``."""
    if name in _KNOWN:
        return _KNOWN[name]
    kind, _, rest = name.partition(":")
    if kind == "boards":
        return " + ".join(part.capitalize() for part in rest.split("+")) + " search"
    if kind == "feed":
        return f"the {rest} feed"
    if kind == "page":
        return rest
    return f"{rest} ({kind})" if rest else name


def source_notice_text(payload: dict[str, Any]) -> str:
    """Telegram HTML for one source notice."""
    name = str(payload.get("source") or "")
    label = escape(source_label(name))
    if payload.get("status") == "up":
        return f"✅ {label} works again."
    error = escape(str(payload.get("error") or "unknown error"))
    if payload.get("permanent"):
        hint = (
            "Check its key in .env." if name in _KEY_SOURCES else "Check the setting that names it in .env."
        )
        return f"⚠️ {label} can't be read: {error}\n{hint} Your other sources keep working."
    streak = int(payload.get("streak") or 0)
    return (
        f"⚠️ {label} has failed {streak} times in a row ({error}).\n"
        "Sarthi keeps trying, less often, and tells you when it works again. Your other sources keep working."
    )
