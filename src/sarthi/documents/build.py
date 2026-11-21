"""Putting the tailored resume and cover letter together from the master resume and the AI's suggestions.

Code owns every fact: name, contact, employers, titles, dates, schools and degrees are copied from the master
resume (Resume-Matcher's "protected fields"); the AI's text is used line by line only when it adds no new
number or skill and has no style problem; any line that fails falls back to the master's own words. With no
AI answer at all, the master resume is used as it is, so a document is always produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sarthi.ai.documents import CoverAnswer, TailorAnswer
from sarthi.documents.truth import fact_problems, style_problems
from sarthi.profile.master import Job, MasterResume, SkillGroup

MIN_BULLETS_KEPT = 2
MAX_BULLETS = 6


@dataclass
class Built:
    """A document and what had to be changed to keep it true."""

    resume: MasterResume
    reverted: list[str] = field(default_factory=list)  # AI lines replaced by the master's own words
    problems: list[str] = field(default_factory=list)  # style problems, to ask the AI again about


def _tidy(text: str) -> str:
    """AI text with its em dashes made plain and whitespace normalised."""
    text = re.sub(r"\s*—\s*", ", ", text)
    return re.sub(r"\s+", " ", text).strip()


class Checker:
    """Checks one AI line against the master resume."""

    def __init__(
        self, master: MasterResume, resume_text: str, own_skills: tuple[str, ...], *, strict: bool = False
    ) -> None:
        """Known facts are ``resume_text`` (the uploaded resume) and the person's skills.

        ``strict`` also replaces lines with style problems (used after the AI had its second try).
        """
        self.known_text = resume_text
        self.skills = (*master.all_skills(), *own_skills)
        self.strict = strict

    def facts(self, text: str) -> list[str]:
        """New numbers or skills in ``text``."""
        return fact_problems(text, self.known_text, self.skills)

    def style(self, text: str) -> list[str]:
        """Clichés, AI talk, em dashes."""
        return style_problems(text)


def _use(candidate: str, fallback: str, where: str, checker: Checker, built: Built) -> str:
    text = _tidy(candidate)
    if not text:
        return fallback
    facts = checker.facts(text)
    if facts:
        built.reverted.append(f"{where}: {'; '.join(facts)}")
        return fallback
    style = checker.style(text)
    if style and checker.strict:
        built.reverted.append(f"{where}: {'; '.join(style)}")
        return fallback
    built.problems += [f"{where} {p}" for p in style]
    return text


def _bullets(job: Job, suggested: list[str], where: str, checker: Checker, built: Built) -> tuple[str, ...]:
    kept: list[str] = []
    for i, bullet in enumerate(suggested[:MAX_BULLETS]):
        text = _use(bullet, "", f"{where} bullet {i + 1}", checker, built)
        if text and text not in kept:
            kept.append(text)
    if len(kept) < min(MIN_BULLETS_KEPT, len(job.bullets)):
        return job.bullets[:MAX_BULLETS]  # too much was untrue: the master's own bullets
    return tuple(kept)


def tailored_resume(
    master: MasterResume,
    answer: TailorAnswer | None,
    resume_text: str,
    own_skills: tuple[str, ...] = (),
    *,
    strict: bool = False,
) -> Built:
    """The resume for one job. Facts from ``master``; wording and order from ``answer`` where it is true."""
    built = Built(master)
    if answer is None:
        return built
    checker = Checker(master, resume_text, own_skills, strict=strict)
    by_index = {item.index: item.bullets for item in answer.experience}
    experience = tuple(
        job.model_copy(update={"bullets": _bullets(job, by_index[i], f"{job.company}", checker, built)})
        if by_index.get(i)
        else job
        for i, job in enumerate(master.experience)
    )
    order = [i for i in dict.fromkeys(answer.project_order) if 0 <= i < len(master.projects)]
    projects = tuple(master.projects[i] for i in order) if order else master.projects
    built.resume = master.model_copy(
        update={
            "headline": _use(answer.headline, master.headline, "headline", checker, built),
            "summary": _use(answer.summary, master.summary, "summary", checker, built),
            "experience": experience,
            "projects": projects,
            "skills": _skills_first(master, answer.skills_first),
        }
    )
    return built


def _skills_first(master: MasterResume, first: list[str]) -> tuple[SkillGroup, ...]:
    """The master's skill groups, with the job's skills (that the person has) moved to the front."""
    have = {s.lower(): s for s in master.all_skills()}
    wanted = [have[s.lower()] for s in first if s.lower() in have]
    if not wanted:
        return master.skills
    rank = {s: i for i, s in enumerate(wanted)}
    groups = []
    for group in master.skills:
        items = sorted(group.items, key=lambda s: rank.get(s, len(rank)))
        groups.append(group.model_copy(update={"items": tuple(items)}))
    groups.sort(key=lambda g: min((rank.get(s, len(rank)) for s in g.items), default=len(rank)))
    return tuple(groups)


# ---- cover letter ---------------------------------------------------------------------------------------


@dataclass
class Letter:
    """A cover letter: greeting, paragraphs, sign-off (greeting and sign-off are written by code)."""

    greeting: str
    paragraphs: list[str]
    sign_off: str
    from_ai: bool
    problems: list[str] = field(default_factory=list)

    def text(self) -> str:
        """The letter as plain text."""
        return "\n\n".join([self.greeting, *self.paragraphs, self.sign_off])


def plain_letter(master: MasterResume, role: str, company: str) -> list[str]:
    """A short, true letter built from the resume alone (used when no AI answer passes the checks)."""
    latest = master.experience[0] if master.experience else None
    skills = ", ".join(master.all_skills()[:4])
    opening = f"I am applying for the {role} role at {company}."
    if latest:
        opening += f" I work as {latest.title} at {latest.company}."
    middle = f"My main skills are {skills}." if skills else ""
    if latest and latest.bullets:
        middle = (middle + " " if middle else "") + f"Recently: {latest.bullets[0].rstrip('.')}."
    closing = "I would welcome a conversation about how I can help your team."
    return [p for p in (opening, middle, closing) if p]


def cover_letter(  # noqa: PLR0913  (all the letter's inputs)
    master: MasterResume,
    answer: CoverAnswer | None,
    resume_text: str,
    *,
    role: str,
    company: str,
    own_skills: tuple[str, ...] = (),
    strict: bool = False,
) -> Letter:
    """The letter for one job.

    Every paragraph must be true (and, when ``strict``, cliché-free); otherwise the plain letter built from
    the resume alone is used.
    """
    sign_off = f"Regards,\n{master.name}" if master.name else "Regards,"
    greeting = f"Dear {company} hiring team,"
    if answer is not None:
        checker = Checker(master, resume_text, own_skills)
        paragraphs = [_tidy(p) for p in answer.paragraphs if _tidy(p)]
        facts = [f for p in paragraphs for f in checker.facts(p)]
        style = [f for p in paragraphs for f in checker.style(p)]
        if paragraphs and not facts and not (strict and style):
            return Letter(greeting, paragraphs, sign_off, from_ai=True, problems=style)
    return Letter(greeting, plain_letter(master, role, company), sign_off, from_ai=False)
