"""Read a source, store what is new, and queue new jobs for matching.

The first successful read of a source only records what is already listed (no alert flood on day one);
later reads queue every new job at alert priority. Catch-up runs pass ``alert=True`` to queue everything
found in the time window regardless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sarthi.db.jobs import JobStore
from sarthi.db.source_state import SourceState, SourceStateStore
from sarthi.jobs.models import JobPosting
from sarthi.jobs.normalize import complete, snippet
from sarthi.log import redact
from sarthi.queue import names
from sarthi.queue.tasks import PermanentError, Priority, TaskQueue
from sarthi.sources.base import SearchQuery, Source

log = logging.getLogger(__name__)

MATCH_TASK = names.MATCH
DOWN_AFTER = 3  # temporary failures in a row before the user is told (checks back off: hours, not minutes)
MAX_NOTICE_ERROR = 200


def _was_reported(state: SourceState) -> bool:
    """True when the user was told this source stopped working (so they should hear it works again)."""
    permanent = (state.last_error or "").startswith(PermanentError.__name__)
    return state.consecutive_failures >= (1 if permanent else DOWN_AFTER)


@dataclass(frozen=True, slots=True)
class IngestReport:
    """What one read of a source found."""

    source: str
    fetched: int
    new: int
    queued: int
    baseline: bool  # True when this read only recorded the starting point


class Ingestor:
    """Reads sources into the job store and queues new jobs for matching."""

    def __init__(
        self, jobs: JobStore, queue: TaskQueue, states: SourceStateStore, *, fill_text: bool = False
    ) -> None:
        """Store jobs in ``jobs``, queue matches on ``queue``, track source health in ``states``.

        With ``fill_text``, a job that arrived without its full description is queued to have its own page
        read first (``fill_text`` task), and is matched after that.
        """
        self.jobs = jobs
        self.queue = queue
        self.states = states
        self.fill_text = fill_text

    async def run(self, source: Source, query: SearchQuery, alert: bool | None = None) -> IngestReport:
        """Fetch ``source`` and store its jobs. ``alert`` overrides the first-run baseline rule when set.

        Source errors are recorded against the source's health and re-raised for the queue to retry or fail.
        """
        state = self.states.get(source.name)
        baseline = state is None or not state.baseline_done
        queue_new = (not baseline) if alert is None else alert

        try:
            postings = await source.fetch(query)
        except Exception as error:
            streak = self.states.record_failure(source.name, f"{type(error).__name__}: {error}")
            log.warning("%s failed (%d in a row): %s", source.name, streak, error)
            permanent = isinstance(error, PermanentError)
            if streak == (1 if permanent else DOWN_AFTER):  # once per outage, not at every failed check
                self._notice(source.name, "down", streak, str(error), permanent=permanent)
            raise
        if state is not None and _was_reported(state):
            self._notice(source.name, "up", 0, "")

        new = queued = 0
        for posting in postings:
            job_id = self._store(source.name, posting)
            if job_id is None:
                continue
            new += 1
            if queue_new:
                task = names.FILL_TEXT if self.fill_text and not posting.description_complete else MATCH_TASK
                self.queue.enqueue(task, job_id, priority=Priority.ALERT)
                queued += 1

        self.states.record_success(source.name)
        report = IngestReport(source.name, len(postings), new, queued, baseline and alert is None)
        log.info("%s: %d fetched, %d new, %d queued", source.name, report.fetched, report.new, report.queued)
        return report

    def _notice(self, source: str, status: str, streak: int, error: str, *, permanent: bool = False) -> None:
        """Queue a message telling the user a source stopped (or started again) working."""
        payload = {
            "source": source,
            "status": status,
            "streak": streak,
            "permanent": permanent,
            "error": redact(error)[:MAX_NOTICE_ERROR],
        }
        self.queue.enqueue(names.SOURCE_NOTICE, f"{source}:{status}", payload, priority=Priority.NORMAL)

    def _store(self, source_name: str, posting: JobPosting) -> str | None:
        """Save one posting lean; the new job's id, or None when it was already known or unusable."""
        filled = complete(posting)
        text, whole = snippet(filled.description)
        # model_copy skips validation; safe here because both values are plain str/bool of the right type.
        complete_text = whole and posting.description_complete  # a source's snippet stays a snippet
        stored = filled.model_copy(update={"description": text, "description_complete": complete_text})
        try:
            result = self.jobs.save(stored, full_text=filled.description)
        except ValueError as error:  # one unusable posting must not drop the batch
            log.debug("%s: skipped %s: %s", source_name, posting.url, error)
            return None
        return result.job_id if result.is_new else None
