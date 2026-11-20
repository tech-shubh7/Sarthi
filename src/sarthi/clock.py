"""Time in one place: an injectable clock and one sortable timestamp format.

Every stored timestamp is UTC text in the same fixed-width format, so SQLite can compare and sort them as
strings. Code takes a ``Clock`` instead of calling ``datetime.now`` so tests can control time.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """The current time, timezone-aware, in UTC."""
    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    """Fixed-width UTC text, e.g. ``2026-11-20T09:30:00.000000Z``. Naive datetimes are rejected."""
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_iso(text: str) -> datetime:
    """Parse text written by ``to_iso`` (or any ISO 8601 with an offset) into an aware UTC datetime."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {text!r}")
    return moment.astimezone(UTC)
