"""AI work for application documents: read the resume into sections, tailor it to a job, write a cover letter.

Patterns copied: ApplyPilot's tailoring (the header and every fact come from code, the model only words and
orders; banned clichés; one fresh retry with the problems listed), OpenApply's ``truthCheck`` (run by the
documents step afterwards) and Resume-Matcher's protected fields. The model never sees contact details: the
resume is scrubbed first, and the letter's greeting and signature are added by code.
"""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from sarthi.ai.client import AiClient, AiError
from sarthi.ai.privacy import scrub
from sarthi.ai.prompts import Prompt
from sarthi.jobs.skills import skills_in
from sarthi.profile.master import MasterResume

log = logging.getLogger(__name__)

RESUME_CHARS = 14_000
JOB_CHARS = 8_000


def at_most(n: int) -> BeforeValidator:
    """A list cut to its first ``n`` items: one item too many from a model must not lose the whole answer."""
    return BeforeValidator(lambda v: v[:n] if isinstance(v, list) else v)


def _or_blank(n: int) -> BeforeValidator:
    """A text longer than ``n`` becomes "", so the code keeps the master's own words for that one line."""
    return BeforeValidator(lambda v: "" if isinstance(v, str) and len(v.strip()) > n else v)


def _paragraph_list(value: object) -> object:
    """Paragraphs given as one string (a small model's habit) split on blank lines."""
    if isinstance(value, str):
        return [p for p in value.split("\n\n") if p.strip()]
    return value


_Strip = StringConstraints(strip_whitespace=True)
Text = Annotated[str, _Strip, _or_blank(600)]
Headline = Annotated[str, _Strip, _or_blank(100)]
Summary = Annotated[str, _Strip, _or_blank(700)]
SkillName = Annotated[str, _Strip, _or_blank(60)]
Paragraph = Annotated[str, _Strip, _or_blank(900)]


# ---- reading the resume into sections -----------------------------------------------------------------

READ_PROMPT = Prompt(
    name="master_resume",
    version="1",
    instructions="""
Copy the resume into JSON sections. Copy text exactly as written: do not reword, summarise, translate or add
anything. Leave out contact details (they are removed anyway).

{"name": "", "headline": "", "summary": "",
 "experience": [{"company": "", "title": "", "location": "", "start": "", "end": "", "bullets": [""]}],
 "projects": [{"name": "", "description": "", "link": "", "bullets": [""]}],
 "education": [{"school": "", "degree": "", "start": "", "end": "", "details": [""]}],
 "skills": [{"label": "", "items": [""]}],
 "extras": [{"heading": "", "items": [""]}]}

Dates as written ("Jun 2023", "2021", "Present"). Skills: one item per tool or skill, grouped as the resume
groups them. Anything else (certifications, awards, languages, publications) goes in "extras".
""",
)


async def read_master(client: AiClient, resume_text: str) -> MasterResume | None:
    """The resume split into sections by the AI (not yet grounded), or None when no model answered."""
    if not client.available or not resume_text.strip():
        return None
    try:
        return await client.structured(
            READ_PROMPT, MasterResume, urgent=False, resume=scrub(resume_text[:RESUME_CHARS])
        )
    except AiError as error:
        log.info("resume not split into sections: %s", error)
        return None


# ---- tailoring to one job ---------------------------------------------------------------------------------


class TailoredJob(BaseModel):
    """New wording for one position's bullets, by position number."""

    model_config = ConfigDict(extra="ignore")

    index: int = Field(ge=0, le=30)
    bullets: Annotated[list[Text], at_most(8)] = Field(default_factory=list)


class TailorAnswer(BaseModel):
    """What the model may change. Everything else is copied from the master resume by code."""

    model_config = ConfigDict(extra="ignore")

    headline: Headline = ""
    summary: Summary = ""
    experience: Annotated[list[TailoredJob], at_most(20)] = Field(default_factory=list)
    project_order: Annotated[list[int], at_most(15)] = Field(default_factory=list)
    skills_first: Annotated[list[SkillName], at_most(25)] = Field(default_factory=list)


# Truthfulness rules adapted from Resume-Matcher (Apache-2.0), apps/backend/app/prompts/templates.py
# (CRITICAL_TRUTHFULNESS_RULES, IMPROVE_RESUME_PROMPT_KEYWORDS) @ 959e458, and the "interview backtrack
# test" from ai-job-search (MIT), .claude/skills/job-application-assistant/03-writing-style.md @ 965452f.
TAILOR_PROMPT = Prompt(
    name="tailor_resume",
    version="4",
    instructions="""
Tailor the candidate's resume to the job the way a careful human editor would: the same person, the same
facts, the same layout, with each line re-worded so the job's must-haves are easy to see. The new text is
printed in the candidate's own resume design, in the space the old line used, so every line keeps its length.

Truth rules (never break them; an untrue line costs the candidate the interview):
1. Add no skill, tool, technology, employer, product, certification, number, team size or result that the
   line does not already state. <job_keywords> lists the job's skills the candidate has ("have") and the
   ones they do not ("lack"): use a "have" word where that line's work really involved it; never a "lack" one.
2. Keep every number exactly as written. Do not raise seniority or scope ("helped" stays "helped", "a team
   of 3" is not "led a team").
3. Each line is about its own work only: never move a detail from one bullet, job or project to another.
4. Interview backtrack test: the candidate must be able to explain every line in an interview without
   saying "well, what I actually meant was...". Using the job's term for the same work is the point of
   tailoring: "built data pipelines that load sales data nightly" can say "ETL pipelines"; "LLM app that
   answers from company documents" can say "RAG pipeline". Claiming adjacent work, or a wider scope, is not.
5. Do not copy the posting's sentences: use its hard keywords exactly, in the candidate's own sentence.

A recruiter scans for six seconds: the summary names the target role and why the candidate fits; the first
bullet of each position and the first three of the latest one show the job's must-haves when the resume has
them. Where a line has no number, keep its outcome in words (who used it, what it replaced, its scope).

Return JSON:
- "summary": the candidate's summary re-worded toward this job: what they do, the job's must-haves they
  really have, one real result. Keep its length (the character count shown, within 10%). Call the
  candidate only by a title the resume gives them (never the job's title if they have not held it). No "I",
  no "seeking", no buzzwords.
- "headline": a short line (max 8 words): a title the resume gives the candidate and 2-3 of their real
  skills the job asks for.
- "experience": for EVERY position, {"index": <its number>, "bullets": [...]} with EXACTLY as many bullets
  as it has now, in the same order: bullet 1 is the new wording of bullet 1, and so on. Each bullet keeps
  its length (the character count shown, within 10%), keeps its numbers, starts with an active past-tense
  verb like the original (never passive: "Search service built in Python" is wrong), and brings the job's
  term for that same work into the sentence.

Example (another candidate, another job asking for "ETL" and "data quality"):
  original (84 chars): Wrote Python scripts that pull sales data from three APIs into Postgres every night.
  good (85 chars):     Wrote nightly Python ETL scripts that load sales data from three APIs into Postgres.
  bad: "ETL pipelines architected for sales data, ensuring data quality" (passive, "architected" and
  "data quality" are claims the original never makes)
- "project_order": project numbers, most relevant first (leave out none)
- "skills_first": the candidate's skills that the job asks for, most important first (copy names exactly)

Write like the candidate, not a template: concrete nouns, short words, plain past-tense verbs (built, cut,
wrote, moved, designed). No filler ("various", "multiple", "responsible for", "worked on"). No em dashes.
Never use: passionate, leverage, spearheaded, orchestrated, robust, cutting-edge, seamless, synergy,
results-driven, detail-oriented, proven track record, dynamic, innovative, utilize, demonstrated.
A line that already reads well and already uses the job's words may stay exactly as it is.
""",
    shape='{"summary": "", "headline": "", "experience": [{"index": 0, "bullets": [""]}], '
    '"project_order": [0], "skills_first": [""]}',
)


def job_keywords(master: MasterResume, job_text: str, own_skills: tuple[str, ...] = ()) -> str:
    """The job's skills split into those the candidate has and those they lack (for the tailoring prompt)."""
    have = [*master.all_skills(), *own_skills]
    asked = skills_in(job_text, have)
    lower = {s.lower() for s in have}
    return (
        f"have: {', '.join(s for s in asked if s.lower() in lower) or 'none'}\n"
        f"lack: {', '.join(s for s in asked if s.lower() not in lower) or 'none'}"
    )


def sections_text(master: MasterResume, *, lengths: bool = False) -> str:
    """The master resume with numbered positions and projects, for prompts (no contact details).

    ``lengths`` adds each line's character count, so a re-worded line can keep its length.
    """

    def size(text: str) -> str:
        return f" ({len(text)} chars)" if lengths else ""

    lines = [f"Headline: {master.headline}", f"Summary{size(master.summary)}: {master.summary}", ""]
    lines.append("Experience:")
    for i, job in enumerate(master.experience):
        lines.append(f"[{i}] {job.title} at {job.company} ({job.start} - {job.end})")
        lines += [f"  {k + 1}.{size(b)} {b}" for k, b in enumerate(job.bullets)]
    lines.append("\nProjects:")
    for i, project in enumerate(master.projects):
        lines.append(f"[{i}] {project.name}: {project.description}")
        lines += [f"  - {b}" for b in project.bullets]
    lines.append("\nSkills: " + ", ".join(master.all_skills()))
    for school in master.education:
        lines.append(f"Education: {school.degree}, {school.school} ({school.start} - {school.end})")
    return "\n".join(lines)


async def tailor(
    client: AiClient,
    master: MasterResume,
    job_text: str,
    problems: list[str] | None = None,
    own_skills: tuple[str, ...] = (),
) -> TailorAnswer | None:
    """The model's tailoring answer, or None when no model answered. ``problems`` asks for a corrected try."""
    if not client.available:
        return None
    feedback = "\n".join(f"- {p}" for p in problems or []) or "none"
    try:
        return await client.structured(
            TAILOR_PROMPT,
            TailorAnswer,
            resume_sections=scrub(sections_text(master, lengths=True)),
            job_keywords=job_keywords(master, job_text, own_skills),
            job_posting=job_text[:JOB_CHARS],
            problems_to_fix=feedback,
        )
    except AiError as error:
        log.info("resume not tailored: %s", error)
        return None


# ---- tailoring as edits (each names its line and quotes it) -----------------------------------------------


ChangeText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1500)]


class ResumeChange(BaseModel):
    """One edit: which line (``path``), what it says now (``original``) and what it should say (``value``)."""

    model_config = ConfigDict(extra="ignore")

    path: Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]
    action: Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=20)] = "replace"
    original: ChangeText | Annotated[list[ChangeText], at_most(20)] | None = None
    value: ChangeText | Annotated[list[ChangeText], at_most(60)] = ""
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""


class ResumeChanges(BaseModel):
    """The model's edits; code checks each one before it is used."""

    model_config = ConfigDict(extra="ignore")

    changes: Annotated[list[ResumeChange], at_most(40)] = Field(default_factory=list)


# Adapted from Resume-Matcher (Apache-2.0), apps/backend/app/prompts/templates.py (DIFF_IMPROVE_PROMPT,
# CRITICAL_TRUTHFULNESS_RULES "heavy", DIFF_STRATEGY_INSTRUCTIONS) @ 959e458. Changed by Sarthi contributors:
# Sarthi's paths, the per-line truth rule (a line may only use its own facts), no added bullets or skills,
# the headline never changed, and bullet lengths kept.
TAILOR_CHANGES_PROMPT = Prompt(
    name="tailor_changes",
    version="3",
    instructions="""
Tailor the candidate's resume in <resume_sections> to the job in <job_posting> by listing targeted changes.
Code applies a change only when it passes every rule below; a change that breaks one is thrown away.

TRUTH RULES (one line at a time):
1. A re-worded line may use ONLY the facts of that same line. Never add a tool, language, framework,
   technology, number, company, product, team size, scope or result that the line itself does not state,
   even when another line, the skills list or the job mentions it. Example: "Built payment APIs in
   Node.js" must not become "Built payment APIs in Python and Node.js" (Python is not in that line).
2. Using the job's word for what the line already says is the point: "LLM workflows with retrieval" may
   say "RAG pipeline"; "nightly scripts that load sales data" may say "ETL jobs". Claiming adjacent work or
   a wider scope is not allowed. <job_keywords> "lack" skills must never appear.
3. Keep every number exactly as written. Do not raise seniority ("worked on" is not "led").
4. Keep each line about its length (the character count shown, within 15%) and start bullets with an
   active past-tense verb, like the original.
5. Never drop, merge or add a bullet, role, project, skill, certification, link or date. Never change
   names, companies, titles, dates, schools or the headline.
6. Do not copy the posting's sentences (no run of 5+ words from it); use its hard keywords in the
   candidate's own sentence. No em dashes. Never use: passionate, leverage, spearheaded, orchestrated,
   robust, cutting-edge, seamless, synergy, results-driven, proven track record, utilize, demonstrated.
7. A line that already reads well for this job stays as it is: do not list it.

CHANGES you can make (copy each path exactly as shown in <resume_sections>):
- "summary", action "replace": the same facts (no fact from elsewhere on the resume), the job's focus
  first. Call the candidate only by a title the resume gives them, never the job's title.
- "experience[i].bullets[j]" or "projects[i].bullets[j]", action "replace": re-word one bullet.
- "experience[i].bullets" or "projects[i].bullets", action "reorder": "value" is ALL that entry's ORIGINAL
  bullets copied exactly, most relevant to this job first.
- "skills[i].items", action "reorder": "value" is ALL skills of that line, the job's skills first.

Every "replace" MUST copy the line's current text exactly into "original" (it is checked). What to send:
1. The summary re-ordered so the facts this job cares about most come first (unless they already do).
2. Every bullet where a job term names work the bullet really describes, re-worded with that term
   (most jobs: 3 to 6 bullets). Keep the bullet's verb strength: "worked on" stays "worked on". Use
   each job term in one or two lines, where it says the most; never inside a product's or feature's
   name ("the Policy AI assistant" stays as named), and never as a label stuck on ("a GenAI pipeline").
3. Each role's bullets and each skills line re-ordered toward the job.
When <problems_to_fix> lists changes of your last answer that were thrown away, give each one again
corrected (or leave it out), plus your other changes.
""",
    shape='{"changes": [{"path": "experience[0].bullets[1]", "action": "replace", '
    '"original": "<the exact current text>", "value": "<the new text>", "reason": "<why>"}, '
    '{"path": "skills[0].items", "action": "reorder", "original": null, "value": ["<skill>"], '
    '"reason": "<why>"}]}',
)


def change_sections(master: MasterResume) -> str:
    """The resume with each changeable line's path and length, for the change prompt (no contact details)."""
    lines = [f"Headline (never changed): {master.headline}"]
    if master.summary:
        lines.append(f"summary ({len(master.summary)} chars): {master.summary}")
    for i, job in enumerate(master.experience):
        lines.append(f"\nexperience[{i}]: {job.title} at {job.company} ({job.start} - {job.end})")
        lines += [f"  experience[{i}].bullets[{k}] ({len(b)} chars): {b}" for k, b in enumerate(job.bullets)]
    for i, project in enumerate(master.projects):
        lines.append(f"\nprojects[{i}]: {project.name} ({project.description})")
        lines += [
            f"  projects[{i}].bullets[{k}] ({len(b)} chars): {b}" for k, b in enumerate(project.bullets)
        ]
    lines.append("")
    for i, group in enumerate(master.skills):
        lines.append(f"skills[{i}].items ({group.label or 'skills'}): {', '.join(group.items)}")
    for school in master.education:
        lines.append(f"Education (never changed): {school.degree}, {school.school}")
    return "\n".join(lines)


async def tailor_changes(  # noqa: PLR0913  (the prompt's inputs)
    client: AiClient,
    master: MasterResume,
    job_text: str,
    problems: list[str] | None = None,
    own_skills: tuple[str, ...] = (),
    *,
    hints: str = "",
) -> ResumeChanges | None:
    """The model's changes for one job, or None when no model answered. ``problems`` asks for a second try.

    ``hints`` names lines that already describe work the job calls by its own word ("path: word" lines).
    """
    if not client.available:
        return None
    feedback = "\n".join(f"- {p}" for p in problems or []) or "none"
    keywords = job_keywords(master, job_text, own_skills)
    if hints:
        keywords += f"\nlines that already describe work the job names (may use that word there):\n{hints}"
    try:
        return await client.structured(
            TAILOR_CHANGES_PROMPT,
            ResumeChanges,
            resume_sections=scrub(change_sections(master)),
            job_keywords=keywords,
            job_posting=job_text[:JOB_CHARS],
            problems_to_fix=feedback,
        )
    except AiError as error:
        log.info("resume changes not made: %s", error)
        return None


# ---- cover letter ---------------------------------------------------------------------------------------


class CoverAnswer(BaseModel):
    """The letter's body paragraphs (greeting and signature are added by code)."""

    model_config = ConfigDict(extra="ignore")

    paragraphs: Annotated[
        list[Paragraph],
        at_most(4),
        BeforeValidator(_paragraph_list),
    ] = Field(min_length=2)


COVER_PROMPT = Prompt(
    name="cover_letter",
    version="4",
    instructions="""
Write the body of a short cover letter (3 paragraphs, 170-240 words in total) in the candidate's own voice,
to the company in <job_posting>, using only facts in <resume_sections>. It should read like a capable person
wrote it themselves for this one job: first person, warm but direct, sentences of different lengths, no
template phrases.

1. Name the role and open with the one thing from the resume that fits this job best, said plainly.
2. Pick one or two specific needs the job posting states and, for each, what the candidate built that
   answers it (from the resume). Keep every number exactly as written; add no new numbers, tools or results.
   Each claim must come from ONE resume line: never combine details of different bullets, projects or jobs,
   and never give the candidate a title they did not hold.
3. One or two sentences: what they would like to work on there (from the posting) and a plain request for a
   conversation.

No greeting, no signature, no subject line, no praise of the company ("industry leader", "renowned"). No em
dashes. Never use: passionate, thrilled, excited to apply, leverage, align with, demonstrate, resonate,
synergy, I believe, I am confident, furthermore, additionally, moreover, dynamic, innovative, esteemed,
I am writing to. Never hedge or apologise for the fit ("the closest thing I have", "although I have not",
"I hope", "I may not"): state what the candidate built and let it speak.

Return JSON: {"paragraphs": ["<paragraph 1>", "<paragraph 2>", "<paragraph 3>"]}
""",
    shape='{"paragraphs": ["<paragraph 1>", "<paragraph 2>", "<paragraph 3>"]}',
)


async def cover_letter(
    client: AiClient, master: MasterResume, job_text: str, problems: list[str] | None = None
) -> CoverAnswer | None:
    """The letter body, or None when no model answered."""
    if not client.available:
        return None
    feedback = "\n".join(f"- {p}" for p in problems or []) or "none"
    try:
        return await client.structured(
            COVER_PROMPT,
            CoverAnswer,
            resume_sections=scrub(sections_text(master)),
            job_posting=job_text[:JOB_CHARS],
            problems_to_fix=feedback,
        )
    except AiError as error:
        log.info("cover letter not written: %s", error)
        return None


# ---- checking claims (an AI reviewer, after the code checks) ---------------------------------------------


class ClaimCheck(BaseModel):
    """Claims in a text that the resume does not support."""

    model_config = ConfigDict(extra="ignore")

    unsupported: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]] = Field(
        default_factory=list, max_length=10
    )


CLAIMS_PROMPT = Prompt(
    name="check_claims",
    version="1",
    instructions="""
You are a strict fact checker. <user_answer> was written about the candidate in <resume_sections>.
List every claim in it that is NOT directly supported by a single line of the resume: a detail moved from
one bullet, project or job to another, two separate facts combined into one, a job title the candidate did
not hold, or anything not in the resume at all. Opinions and the job's own requirements are not claims.

Return JSON: {"unsupported": ["<short quote of the claim> - <why>", ...]} ([] when everything is supported).
""",
    shape='{"unsupported": ["<short quote of the claim> - <why>"]}',
)


async def unsupported_claims(client: AiClient, master: MasterResume, text: str) -> list[str] | None:
    """Claims in ``text`` the resume does not support; None when no model answered."""
    if not client.available or not text.strip():
        return [] if text.strip() == "" else None
    try:
        answer = await client.structured(
            CLAIMS_PROMPT, ClaimCheck, resume_sections=scrub(sections_text(master)), user_answer=text[:4000]
        )
    except AiError as error:
        log.info("claims not checked: %s", error)
        return None
    return answer.unsupported
