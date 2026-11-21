"""The ``fill_text`` task: read a job's full description from its own page, then queue it for matching.

Jobs from the company-board snapshot, Adzuna and Jooble arrive with a title or a snippet only. Matching
on that misses what decides fit (years asked, must-have skills), so the job's own page is read first.
Whatever happens, the job is matched afterwards: a page that cannot be used (robots.txt, a login wall,
gone) leaves it with what it had, and a site that keeps failing is given up on after the last attempt.
"""

from __future__ import annotations

import logging

from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.queue import names
from sarthi.queue.tasks import MAX_ATTEMPTS, Priority, RetryableError, TaskQueue
from sarthi.sources.fulltext import Fetcher

log = logging.getLogger(__name__)


class TextFiller:
    """Handles ``fill_text`` tasks."""

    def __init__(self, jobs: JobStore, queue: TaskQueue, fetcher: Fetcher) -> None:
        """Read jobs from ``jobs``, pages with ``fetcher``; queue matching on ``queue``."""
        self.jobs = jobs
        self.queue = queue
        self.fetcher = fetcher

    async def handle(self, task: TaskRecord) -> None:
        """Fill the job named by ``task.key``, then queue its match."""
        job = self.jobs.get(task.key)
        if job is None:
            return
        try:
            page = await self.fetcher.fetch(str(job.url))
        except RetryableError:
            if task.attempts < MAX_ATTEMPTS:
                raise
            page = None  # the site kept failing: match on what the job has
        if page is not None and page.text and len(page.text) > len(self.jobs.full_text(task.key)):
            self.jobs.set_full_text(task.key, page.text)
        elif page is not None:
            log.debug("%s: page not used (%s)", task.key, page.note or "no longer text than before")
        self.queue.enqueue(names.MATCH, task.key, priority=Priority.ALERT)

    async def aclose(self) -> None:
        """Close the page fetcher."""
        await self.fetcher.aclose()
