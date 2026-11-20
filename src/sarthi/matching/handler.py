"""The ``match`` task: run the funnel for one job, store the result, and queue an alert for strong matches."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sarthi.ai.scoring import AiScorer
from sarthi.clock import Clock, utcnow
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchRecord, MatchStore
from sarthi.db.tasks import TaskRecord
from sarthi.matching.decide import Decision, Thresholds
from sarthi.matching.funnel import evaluate
from sarthi.profile.models import Profile
from sarthi.queue.tasks import DeferError, PermanentError, Priority, TaskQueue

log = logging.getLogger(__name__)

ALERT_TASK = "alert"
WAIT_FOR_PROFILE_S = 3600


class Matcher:
    """Handles ``match`` tasks."""

    def __init__(  # noqa: PLR0913  (explicit dependencies; all keyword-only after the stores)
        self,
        jobs: JobStore,
        matches: MatchStore,
        queue: TaskQueue,
        load_profile: Callable[[], Profile | None],
        *,
        thresholds: Thresholds = Thresholds(),  # noqa: B008  (frozen dataclass, safe default)
        ai: AiScorer | None = None,
        clock: Clock = utcnow,
    ) -> None:
        """Read jobs from ``jobs``, write results to ``matches``, queue alerts on ``queue``."""
        self.jobs = jobs
        self.matches = matches
        self.queue = queue
        self._load_profile = load_profile
        self.thresholds = thresholds
        self.ai = ai
        self._clock = clock

    async def handle(self, task: TaskRecord) -> None:
        """Match the job named by ``task.key``."""
        profile = self._load_profile()
        if profile is None:
            raise DeferError(WAIT_FOR_PROFILE_S)  # not set up yet: keep the job, try again later
        job = self.jobs.get(task.key)
        if job is None:
            raise PermanentError(f"job {task.key} no longer exists")
        if full_text := self.jobs.full_text(task.key):
            # model_copy skips validation; safe here because the value is a plain str.
            job = job.model_copy(update={"description": full_text, "description_complete": True})

        result = await evaluate(job, profile, self._clock(), self.thresholds, self.ai)
        self.matches.save(
            MatchRecord(
                job_id=task.key,
                profile_version=profile.version,
                score=result.score,
                decision=result.decision.value,
                rule=result.outcome.rule,
                hidden_because=result.outcome.hidden_because,
                reasons=result.reasons,
                notes=result.notes,
                ai_used=result.ai_used,
            )
        )
        if result.decision is Decision.ALERT:
            self.queue.enqueue(ALERT_TASK, task.key, priority=Priority.ALERT)
        log.debug("%s %s: %s (%d)", job.company, job.title, result.decision, result.score)
