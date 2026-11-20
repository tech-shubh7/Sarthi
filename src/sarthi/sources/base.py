"""What every job source looks like, and what it is asked for (docs/ARCHITECTURE.md §2.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sarthi.jobs.models import JobPosting


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """What the user is looking for, as far as a source can use it.

    Sources that list everything (a company's own job board) ignore ``roles``; matching filters later.
    ``hours_old`` limits results to jobs posted in that window (catch-up mode, docs/ARCHITECTURE.md §2.18).
    """

    roles: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()  # ISO 3166-1 alpha-2
    remote: bool = False
    hours_old: int | None = None
    results_per_search: int = 50
    extra: dict[str, str] = field(default_factory=dict)


class Source(Protocol):
    """A job source. ``name`` is stable and unique; it keys the source's state in the database."""

    @property
    def name(self) -> str:
        """Stable id, e.g. ``"greenhouse:razorpay"`` or ``"boards:linkedin"``."""
        ...

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """Return the jobs currently listed.

        Raise ``sarthi.queue.tasks.RetryableError`` for temporary failures and ``PermanentError`` when the
        source cannot work (e.g. the company board no longer exists).
        """
        ...
