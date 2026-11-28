"""Layer 2 of the match funnel: the AI reads the full job and the user's profile.

Design follows JobRadar (MIT): the model answers typed questions, each 0-1, and code combines them into
the score with fixed weights; the model's own "overall" is one input, not the verdict. Hard eligibility
(country, visa, job type) stays with the rules layer, which runs before this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sarthi.ai.client import AiClient, AiError
from sarthi.ai.documents import at_most
from sarthi.ai.privacy import scrub
from sarthi.ai.prompts import Prompt
from sarthi.jobs.models import JobPosting
from sarthi.jobs.requirements import too_experienced_for
from sarthi.profile.models import Profile

log = logging.getLogger(__name__)

RESUME_CHARS = 6000
JOB_CHARS = 8000
NOT_ELIGIBLE_BELOW = 0.5
NOT_ELIGIBLE_FACTOR = 0.3
LEVEL_MISMATCH_BELOW = 0.35  # the AI says the job's level is far from the candidate's experience
SURE_ENOUGH = 0.6  # a level judgement from a vague or empty posting is not used to hide a job
# Rubric first, as in jobtracker and HireEx: the model checks each must-have against the resume and code turns
# that into the score; its own "overall" is one small input. Anchored examples keep "role fit" steady.
_WEIGHTS = {"role_fit": 0.30, "coverage": 0.35, "seniority_fit": 0.20, "overall_fit": 0.15}
WRONG_ROLE_BELOW = 0.4  # not the kind of work the resume shows: capped, whatever the keywords
WRONG_ROLE_CAP = 0.45  # under the hide line (50): never shown
THIN_COVERAGE_BELOW = 0.5  # fewer than half the must-haves shown on the resume
THIN_COVERAGE_CAP = 0.55
VAGUE_CAP = 0.7  # the model is unsure (a vague posting): never shown as a near-perfect match
TOP_MISSING_CAP = 0.7  # the first (most important) must-have is not on the resume: never an alert
MAX_MUST_HAVES = 6

MAX_LINES = 2
# Marks a reason that is something missing; chat cards show it with ✗ instead of ✓. Stored with the match.
GAP = "✗ "

Unit = Annotated[float, Field(ge=0, le=1)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]


@dataclass(frozen=True, slots=True)
class AiScore:
    """The AI layer's verdict: 0-100 fit, how sure it is (0-1), and its reasons."""

    score: int
    confidence: float
    reasons: tuple[str, ...] = ()
    blocker: str | None = None  # why the job is out of reach, judged from the full posting


class AiScorer(Protocol):
    """Layer 2. Returns None when it cannot answer (no model set up, budget spent, provider down)."""

    @property
    def available(self) -> bool:
        """True when a model is set up (it may still fail to answer right now)."""
        ...

    async def score(self, job: JobPosting, profile: Profile) -> AiScore | None:
        """Judge how well ``job`` fits ``profile``."""
        ...


class MustHave(BaseModel):
    """One requirement the posting states as required, and whether the resume shows it."""

    model_config = ConfigDict(extra="ignore")

    need: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]
    shown: bool


class MatchAnswers(BaseModel):
    """The JSON shape the model must return."""

    model_config = ConfigDict(extra="ignore")

    eligible: Unit = Field(
        description="Could the candidate realistically be hired, from stated requirements?"
    )
    role_fit: Unit
    skills_fit: Unit
    seniority_fit: Unit
    overall_fit: Unit
    confidence: Unit
    min_years_required: float | None = Field(default=None, ge=0, le=40)
    must_haves: Annotated[list[MustHave], at_most(MAX_MUST_HAVES)] = Field(default_factory=list)
    strengths: Annotated[list[Reason], at_most(MAX_LINES)] = Field(default_factory=list)
    gaps: Annotated[list[Reason], at_most(MAX_LINES)] = Field(default_factory=list)


MATCH_PROMPT = Prompt(
    name="match",
    version="6",
    instructions="""
Judge how well the job fits the candidate, as a strict recruiter would. Use only what the job posting and
the candidate's resume say; a skill counts only when the resume shows it was used.

Return JSON with these keys, each a number from 0 to 1 unless stated:
- "must_haves": up to 6 requirements the posting states as REQUIRED (not "nice to have"), most important
  first, each {"need": "<short name>", "shown": true or false}; "shown" is true only when the resume shows
  that skill or experience in use. An empty list when the posting states none.
- "role_fit": is this the kind of work the resume shows they do AND want (target roles). Examples:
  * the same kind of role on the same stack: 0.9-1.0
  * a general "Software Engineer" role whose stack matches: 0.7
  * an engineer building LLM apps, agents and RAG, applying to "ML Engineer" work that trains models
    (feature engineering, statistics, model training, fraud or recommendation models): 0.3
  * a Python backend developer applying to a Java/Spring, frontend-only, data-analyst or DevOps role: 0.2
  * a "Full Stack" or "Software Engineer" title whose duties are mostly UI work (React screens, CSS), or
    mostly Salesforce, ServiceNow, SAP or mobile apps, for a backend candidate: 0.3
  * sales, support, teaching or other non-engineering work: 0.0
  Judge the duties and requirements, not the title: the title often says less than the text.
- "skills_fit": share of the job's required skills the resume shows
- "seniority_fit": does the job's level match their experience (a senior, "III", staff or lead role for
  someone with 1-2 years is 0.1 or lower; a role asking for 2+ years more than they have is 0.4 or lower;
  duties such as owning the roadmap, mentoring engineers or leading design reviews are senior duties).
  The years the posting states decide over a grade in the title: "Software Engineer II" asking for 1 year
  fits someone with 1 year. An entry-level role (SDE I, Associate, Junior, Graduate, Fresher, 0-2 years)
  for someone with 0-2 years is 0.9 or higher.
- "eligible": could they realistically be hired, judging by required degrees, years and must-haves
- "overall_fit": your overall judgement
- "confidence": how sure you are: 0.4 or lower when the posting is vague, very short, cut off before its
  requirements, only the company's description, or lists only generic requirements ("knowledge of
  programming languages", "good communication")
- A tool the candidate would use while working (Cursor, Claude Code, GitHub Copilot, Jira) is not a
  must-have skill.
- "min_years_required": the minimum years of work experience the posting requires (a number, e.g. 5 for
  "5-8 years", 0 for "0-2 years"), or null when it states none
- "strengths": up to 2 short sentences, most important first: what the candidate has that the job asks
  for, naming the skills
- "gaps": up to 2 short sentences: what the job requires that the resume does not show (missing skills,
  fewer years than asked); an empty list when nothing important is missing
Do not mention location, visa or salary. Never put a gap in "strengths".
""",
    shape=(
        '{"must_haves": [{"need": "...", "shown": true}], "role_fit": 0.0, "skills_fit": 0.0, '
        '"seniority_fit": 0.0, "eligible": 0.0, "overall_fit": 0.0, "confidence": 0.0, '
        '"min_years_required": null, "strengths": ["..."], "gaps": ["..."]}'
    ),
)


def _candidate_text(profile: Profile) -> str:
    prefs = profile.preferences
    years = f"{prefs.years_experience:g}" if prefs.years_experience is not None else "not stated"
    return (
        f"Target roles: {', '.join(prefs.roles)}\n"
        f"Skills: {', '.join((*profile.skills, *prefs.keywords)) or 'not stated'}\n"
        f"Years of experience: {years}"
    )


def _preferences_text(profile: Profile) -> str:
    prefs = profile.preferences
    parts = [
        f"Looking for: {prefs.looking_for}" if prefs.looking_for else "",
        f"Rules out: {prefs.ruled_out}" if prefs.ruled_out else "",
    ]
    return "\n".join(p for p in parts if p) or "Nothing beyond the target roles."


def coverage(answers: MatchAnswers) -> float:
    """Share of the posting's must-haves the resume shows; the model's skills_fit when it listed none."""
    if not answers.must_haves:
        return answers.skills_fit
    return sum(m.shown for m in answers.must_haves) / len(answers.must_haves)


def combine(answers: MatchAnswers) -> int:
    """The 0-100 score from the model's answers.

    Capped when the role is not the candidate's kind of work or most must-haves are missing, however well
    the rest reads; a probably-ineligible job is scaled down hard.
    """
    parts = {
        "role_fit": answers.role_fit,
        "coverage": coverage(answers),
        "seniority_fit": answers.seniority_fit,
        "overall_fit": answers.overall_fit,
    }
    raw = sum(parts[name] * weight for name, weight in _WEIGHTS.items())
    if answers.role_fit < WRONG_ROLE_BELOW:
        raw = min(raw, WRONG_ROLE_CAP)
    if parts["coverage"] < THIN_COVERAGE_BELOW:
        raw = min(raw, THIN_COVERAGE_CAP)
    if answers.must_haves and not answers.must_haves[0].shown:
        # The posting's most important requirement is not on the resume (a required BigCommerce, n8n): a fair
        # match at most, however many of the rest are (audit: such jobs were alerted at 77-89).
        raw = min(raw, TOP_MISSING_CAP)
    if answers.confidence < SURE_ENOUGH:
        raw = min(raw, VAGUE_CAP)
    if answers.eligible < NOT_ELIGIBLE_BELOW:
        raw *= NOT_ELIGIBLE_FACTOR
    return max(0, min(100, round(raw * 100)))


def gap_lines(answers: MatchAnswers) -> tuple[str, ...]:
    """The model's gaps, or (when it gave none) the must-haves the resume does not show."""
    if answers.gaps:
        return tuple(answers.gaps)
    missing = [m.need for m in answers.must_haves if not m.shown]
    return (f"Not shown on your resume: {', '.join(missing[:4])}",) if missing else ()


def blocker_for(answers: MatchAnswers, years: float | None) -> str | None:
    """Why the posting is out of reach for someone with ``years`` of experience, from the AI's reading."""
    if years is None:
        return None
    needed = answers.min_years_required
    if needed is not None and too_experienced_for(needed, years):
        return f"Asks for {needed:g}+ years of experience; you have {years:g}"
    if answers.seniority_fit < LEVEL_MISMATCH_BELOW and answers.confidence >= SURE_ENOUGH:
        return f"Level is well above your {years:g} year(s) of experience"
    return None


class LlmMatchScorer:
    """Scores jobs with the configured AI models."""

    def __init__(self, client: AiClient) -> None:
        """Ask ``client``'s models."""
        self._client = client

    @property
    def available(self) -> bool:
        """True when the client has a model to ask."""
        return self._client.available

    async def score(self, job: JobPosting, profile: Profile) -> AiScore | None:
        """The AI's verdict, or None when no model can answer right now."""
        if not self._client.available:
            return None
        posting = f"Title: {job.title}\nCompany: {job.company}\n\n{job.description[:JOB_CHARS]}"
        try:
            answers = await self._client.structured(
                MATCH_PROMPT,
                MatchAnswers,
                job_posting=posting,
                candidate=_candidate_text(profile),
                preferences=_preferences_text(profile),
                resume=scrub(profile.resume_text[:RESUME_CHARS]) or "No resume text.",
            )
        except AiError as error:
            log.info("AI score skipped for %s: %s", job.url, error)
            return None
        blocker = blocker_for(answers, profile.preferences.years_experience)
        reasons = (*answers.strengths, *(GAP + gap for gap in gap_lines(answers)))
        return AiScore(combine(answers), answers.confidence, reasons, blocker)
