"""When each job source is read (docs/ARCHITECTURE.md §2.1 "smart polling", §2.18 catch-up).

Design follows JobRadar's board watcher: a tick every minute queues a ``source_check`` for each source that is
due. Each check looks back to its last successful read, so time the computer was off is filled in
automatically. A failing source waits longer after each failure (up to 8x its interval) instead of hammering
the site, and recovers on its own.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sarthi.clock import Clock, from_iso, utcnow
from sarthi.db.source_state import SourceState, SourceStateStore
from sarthi.db.tasks import TaskRecord
from sarthi.profile.models import Profile, RemotePreference
from sarthi.queue import names
from sarthi.queue.tasks import PermanentError, Priority, RetryableError, TaskQueue
from sarthi.sources.ats import CompanyBoardSource
from sarthi.sources.base import SearchQuery, Source
from sarthi.sources.boards import JobBoardSource
from sarthi.sources.ingest import Ingestor

log = logging.getLogger(__name__)

FIRST_LOOK_BACK_HOURS = {"boards": 24, "company": 72}
MAX_LOOK_BACK_HOURS = 168
LOOK_BACK_MARGIN_HOURS = 1
MAX_BACKOFF_FACTOR = 8


@dataclass(frozen=True, slots=True)
class Planned:
    """One source and how often to read it."""

    source: Source
    kind: str  # "boards" or "company"
    every: timedelta


def plan_sources(
    job_boards: list[str],
    watch_companies: list[str],
    boards_every: timedelta,
    companies_every: timedelta,
) -> list[Planned]:
    """The sources to watch. Company entries look like ``"greenhouse/razorpay"``; bad ones are skipped."""
    planned: list[Planned] = []
    if job_boards:
        planned.append(Planned(JobBoardSource(tuple(job_boards)), "boards", boards_every))
    for entry in watch_companies:
        system, slash, slug = entry.strip().partition("/")
        if not slash or not system or not slug:
            log.warning(
                "company board %r skipped: use <hiring system>/<company>, e.g. greenhouse/razorpay", entry
            )
            continue
        planned.append(Planned(CompanyBoardSource(system.lower(), slug), "company", companies_every))
    return planned


def look_back_hours(kind: str, state: SourceState | None, now: datetime) -> int:
    """Hours to search back: since the last successful read (plus a margin), or a first-run window."""
    if state is None or state.last_ok_at is None:
        return FIRST_LOOK_BACK_HOURS[kind]
    since = (now - from_iso(state.last_ok_at)).total_seconds() / 3600
    return max(1, min(MAX_LOOK_BACK_HOURS, math.ceil(since) + LOOK_BACK_MARGIN_HOURS))


def query_for(profile: Profile | None, kind: str, hours_old: int) -> SearchQuery | None:
    """What to ask a source for; None when a board search has no profile (and so no roles) yet."""
    if kind == "company":
        return SearchQuery(hours_old=hours_old)
    if profile is None:
        return None
    prefs = profile.preferences
    # Remote-only still searches the home country ("remote jobs in India"): with no location the boards
    # default to the United States.
    only_remote = prefs.remote is RemotePreference.ONLY
    countries = (prefs.home_country,) if only_remote else tuple(sorted(prefs.work_countries))
    return SearchQuery(
        roles=prefs.roles,
        countries=countries,
        remote=only_remote,
        hours_old=hours_old,
    )


class SourceScheduler:
    """Queues source checks when they are due, and runs them."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        planned: list[Planned],
        ingestor: Ingestor,
        states: SourceStateStore,
        queue: TaskQueue,
        load_profile: Callable[[], Profile | None],
        *,
        clock: Clock = utcnow,
    ) -> None:
        """Watch ``planned`` sources, storing jobs through ``ingestor``."""
        self.planned = {p.source.name: p for p in planned}
        self.ingestor = ingestor
        self.states = states
        self.queue = queue
        self._load_profile = load_profile
        self._clock = clock
        current = load_profile()
        self._profile_version = current.version if current else None  # a restart alone forces nothing

    def _due(self, planned: Planned, state: SourceState | None, now: datetime) -> bool:
        if state is None or state.last_checked_at is None:
            return True
        factor: int = min(2**state.consecutive_failures, MAX_BACKOFF_FACTOR)
        next_check: datetime = from_iso(state.last_checked_at) + planned.every * factor
        return next_check <= now

    def tick(self) -> int:
        """Queue every source that is due. Returns how many were queued."""
        now = self._clock()
        profile = self._load_profile()
        changed = profile is not None and profile.version != self._profile_version
        self._profile_version = profile.version if profile else None
        queued = 0
        for name, planned in self.planned.items():
            if planned.kind == "boards" and profile is None:
                continue  # board searches need the user's roles
            new_search = planned.kind == "boards" and changed  # new roles or countries: search now
            if new_search or self._due(planned, self.states.get(name), now):
                self.queue.enqueue(names.SOURCE_CHECK, name, priority=Priority.NORMAL)
                queued += 1
        return queued

    async def handle(self, task: TaskRecord) -> None:
        """``source_check`` task: read one source; its health decides when it runs again."""
        planned = self.planned.get(task.key)
        if planned is None:
            return  # no longer configured
        hours = look_back_hours(planned.kind, self.states.get(task.key), self._clock())
        query = query_for(self._load_profile(), planned.kind, hours)
        if query is None:
            return
        try:
            report = await self.ingestor.run(planned.source, query, alert=True)
        except (RetryableError, PermanentError) as error:
            # Already recorded against the source's health; the next tick retries after a longer wait.
            log.warning("%s will be retried later: %s", task.key, error)
            return
        log.info("%s: %d new job(s) in the last %d h", task.key, report.new, hours)
