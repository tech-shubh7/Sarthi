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
from sarthi.documents.truth import (
    echo_problems,
    fact_problems,
    opening_problems,
    style_problems,
    title_problems,
    verb_upgrade,
)
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
    text = re.sub(r"[\u2010-\u2012]", "-", text)  # models' "non-breaking" hyphens; \s takes U+00A0
    return re.sub(r"\s+", " ", text).strip()


class Checker:
    """Checks one AI line against the master resume."""

    def __init__(
        self,
        master: MasterResume,
        resume_text: str,
        own_skills: tuple[str, ...],
        *,
        strict: bool = False,
        job_text: str = "",
    ) -> None:
        """Known facts are ``resume_text`` (the uploaded resume) and the person's skills.

        ``strict`` also replaces lines with style problems (used after the AI had its second try).
        ``job_text`` is the posting, so a line that copies its sentences is caught.
        """
        self.known_text = resume_text
        self.skills = (*master.all_skills(), *own_skills)
        self.strict = strict
        self.job_text = job_text

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
    facts = checker.facts(text) + title_problems(
        text, checker.known_text
    )  # a resume line, never the job's title
    if facts:
        built.reverted.append(f"{where}: {'; '.join(facts)}")
        built.problems += [f"{where} {p}" for p in facts]
        return fallback
    style = checker.style(text)
    if style and checker.strict:
        built.reverted.append(f"{where}: {'; '.join(style)}")
        return fallback
    built.problems += [f"{where} {p}" for p in style]
    return text


LONGER = 1.10  # a re-worded line may be 10% longer (it is printed in the space the old line used) ...
LONGER_CHARS = 12  # ... or a few characters, for short lines
SHORTER = 0.6  # and not much shorter: a line cut to half has lost what it said


def length_problem(new: str, old: str) -> str:
    """Why ``new`` cannot take ``old``'s place in the person's own layout ("" when it can)."""
    if len(new) > max(len(old) * LONGER, len(old) + LONGER_CHARS):
        return f"is {len(new)} characters, the original line is {len(old)}: keep its length"
    if len(new) < len(old) * SHORTER:
        return f"is {len(new)} characters, the original line is {len(old)}: say all it said"
    return ""


def _fitted(candidate: str, original: str, where: str, checker: Checker, built: Built) -> str:
    """``candidate`` when it is true, written fresh and fits ``original``'s space, else ``original``."""
    upgraded = verb_upgrade(_tidy(candidate), original)
    if upgraded:  # a level the person did not hold: never used
        built.reverted.append(f"{where}: {'; '.join(upgraded)}")
        built.problems += [f"{where} {p}" for p in upgraded]
        return original
    text = _use(candidate, original, where, checker, built)
    if text == original:
        return text
    problems = [
        p for p in echo_problems(text, checker.job_text) if not echo_problems(original, checker.job_text)
    ]
    problems += [p for p in [length_problem(text, original)] if p] + opening_problems(text, original)
    if problems:
        built.problems += [f"{where} {p}" for p in problems]
        if checker.strict:
            built.reverted.append(f"{where}: {'; '.join(problems)}")
            return original
    return text


def _bullets(job: Job, suggested: list[str], where: str, checker: Checker, built: Built) -> tuple[str, ...]:
    """Each bullet re-worded in its own place: the person's resume keeps its bullets, order and layout."""
    if len(suggested) != len(job.bullets):
        built.problems.append(
            f"{where}: gave {len(suggested)} bullets for {len(job.bullets)}; give exactly {len(job.bullets)}"
        )
        return job.bullets
    return tuple(
        _fitted(new, old, f"{where} bullet {i + 1}", checker, built)
        for i, (new, old) in enumerate(zip(suggested, job.bullets, strict=True))
    )


def tailored_resume(  # noqa: PLR0913  (the resume's inputs)
    master: MasterResume,
    answer: TailorAnswer | None,
    resume_text: str,
    own_skills: tuple[str, ...] = (),
    *,
    strict: bool = False,
    job_text: str = "",
) -> Built:
    """The resume for one job. Facts from ``master``; wording and order from ``answer`` where it is true.

    Every position keeps all its bullets in their order and every line keeps about its length, so the same
    text can be written into the person's own resume file (``edits``) as well as Sarthi's layout.
    """
    built = Built(master)
    if answer is None:
        return built
    checker = Checker(master, resume_text, own_skills, strict=strict, job_text=job_text)
    by_index = {item.index: item.bullets for item in answer.experience}
    built.problems += [
        f"position {i} ({job.company}) is missing: give all {len(job.bullets)} of its bullets"
        for i, job in enumerate(master.experience)
        if job.bullets and not by_index.get(i)
    ]
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
            "summary": _fitted(answer.summary, master.summary, "summary", checker, built)
            if master.summary
            else "",
            "experience": experience,
            "projects": projects,
            "skills": _skills_first(master, answer.skills_first),
        }
    )
    return built


@dataclass(frozen=True, slots=True)
class Edit:
    """One line of the person's resume and its tailored wording."""

    old: str
    new: str
    where: str


def edits(master: MasterResume, tailored: MasterResume) -> list[Edit]:
    """What changed between the master resume and its tailored version, line by line.

    The summary, each bullet in its place and each skills line re-ordered: the text to replace in the
    person's own resume file. Lines left as they were are not listed.
    """
    found: list[Edit] = []
    if tailored.summary and tailored.summary != master.summary:
        found.append(Edit(master.summary, tailored.summary, "summary"))
    for job, new_job in zip(master.experience, tailored.experience, strict=False):
        if len(job.bullets) != len(new_job.bullets):
            continue
        for k, (old, new) in enumerate(zip(job.bullets, new_job.bullets, strict=True)):
            if old != new:
                found.append(Edit(old, new, f"{job.company} bullet {k + 1}"))
    new_groups = {g.label: g.items for g in tailored.skills}
    for group in master.skills:
        items = new_groups.get(group.label, group.items)
        if items != group.items and sorted(items) == sorted(group.items):
            found.append(Edit(", ".join(group.items), ", ".join(items), f"skills {group.label}".strip()))
    return found


SUMMARY_WORDS = 50
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def short_summary(text: str, max_words: int = SUMMARY_WORDS) -> str:
    """The person's own summary cut to whole sentences, at most two and about ``max_words`` words.

    A recruiter reads the summary in seconds; a five-line one that repeats the first bullet is skipped.
    """
    sentences = [s.strip() for s in _SENTENCE_END.split(text.strip()) if s.strip()]
    kept: list[str] = []
    for sentence in sentences[:2]:
        if kept and len(" ".join([*kept, sentence]).split()) > max_words:
            break
        kept.append(sentence)
    return " ".join(kept)


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


MIN_LETTER_PARAGRAPHS = 2


def _as_clause(text: str) -> str:
    """A resume line read on inside a sentence: 'Built an API.' becomes 'built an API'."""
    text = text.strip().rstrip(".")
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def _and_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _short_role(role: str) -> str:
    """A title as said in a sentence: "Backend Developer (Node.js)" -> "Backend Developer"."""
    short = re.sub(r"\s*[(\[][^)\]]*[)\]]", "", role)
    short = re.split(r"\s+(?:\||-|\u2013|\u2014|/)\s+", short)[0]
    return re.sub(r"\s+", " ", short).strip(" ,-|") or role.strip()


def _first_sentence(text: str) -> str:
    """The first sentence of a resume line ("Built X. Rebuilt it on Y." -> "Built X.")."""
    return re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip(), maxsplit=1)[0]


def _mentioned(skill: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w.+#]){re.escape(skill)}(?![\w+#])", text, re.IGNORECASE))


def _relevant_lines(master: MasterResume, job_text: str, n: int = 2) -> list[tuple[str, str]]:
    """The ``n`` resume bullets (with their employer) that name most of what the job asks for."""
    asked = [skill for skill in master.all_skills() if _mentioned(skill, job_text)]
    words = set(re.findall(r"[a-z][a-z+#.]{4,}", job_text.lower()))
    scored = []
    for rank, job in enumerate(master.experience):
        for order, bullet in enumerate(job.bullets):
            hits = sum(_mentioned(skill, bullet) for skill in asked)
            overlap = len(words & set(re.findall(r"[a-z][a-z+#.]{4,}", bullet.lower())))
            scored.append((-(hits * 3 + overlap), rank, order, job.company, bullet))
    scored.sort()
    return [(company, bullet) for *_, company, bullet in scored[:n]]


def plain_letter(master: MasterResume, role: str, company: str, job_text: str = "") -> list[str]:
    """A short, true letter built from the resume alone (used only when no AI letter passes the checks).

    It names the skills the job asks for that the person has, then the two resume lines that show them best,
    each said once and in the person's own words.
    """
    latest = master.experience[0] if master.experience else None
    company = letter_company(company)
    opening = f"I'd like to apply for the {_short_role(role)} role" + (f" at {company}." if company else ".")
    if latest:
        opening += f" I'm currently {_article(latest.title)} {latest.title} at {latest.company}."
    every = master.all_skills()
    asked = [_in_sentence(s) for s in every if _mentioned(s, job_text)][:4]
    middle = f"Your posting asks for {_and_list(asked)}, which is the work I do." if len(asked) >= 2 else ""  # noqa: PLR2004
    for i, (employer, bullet) in enumerate(_relevant_lines(master, job_text)):
        lead = f"At {employer} I" if i == 0 else "I also"
        middle += f" {lead} {_as_clause(_first_sentence(bullet))}."
    if not middle and master.projects and master.projects[0].description:
        project = master.projects[0]
        middle = f"Outside work I built {project.name}, which {_lower_rest(project.description)}."
    closing = (
        f"I'd be glad to talk about how I could help the team{f' at {company}' if company else ''}. "
        "Thank you for your time."
    )
    return [p.strip() for p in (opening, middle, closing) if p.strip()]


def _article(title: str) -> str:
    """The article a title is said with: "an Audit Manager", "a Senior Nurse", "an HR Lead", "a UX Lead"."""
    first = title.split(maxsplit=1)[0] if title.split() else ""
    spoken_vowel = first[:1].lower() in "aeio" or (first[:1].lower() == "u" and not first.isupper())
    if first.isupper() and len(first) > 1:  # an acronym is read letter by letter: "an HR", "an MBA"
        spoken_vowel = first[0] in "AEFHILMNORSX"
    return "an" if spoken_vowel else "a"


def _in_sentence(skill: str) -> str:
    """A skill as written inside a sentence: "Statutory audit" -> "statutory audit"; names stay."""
    words = skill.split()
    if len(words) > 1 and words[0][1:].islower() and all(w.islower() for w in words[1:]):
        return skill[0].lower() + skill[1:]
    return skill


def _lower_rest(description: str) -> str:
    """A project description read on after "which": "Built a tool that..." -> "is a tool that..."."""
    text = description.strip().rstrip(".")
    built = re.match(r"(?i)^(built|created|made|developed|designed)\s+(a|an|the)\s+", text)
    return f"is {built.group(2).lower()} {text[built.end() :]}" if built else _as_clause(text)


_LEGAL_SUFFIX = re.compile(
    r"[\s,]+(?:private\s+limited|pvt\.?\s*ltd\.?|limited|ltd\.?|inc\.?|l\.?l\.?c\.?|l\.?l\.?p\.?"
    r"|gmbh(?:\s*&\s*co\.?\s*kg)?|corp\.?|co\.|ag|se|kg|plc|pty|b\.\s?v\.?|bv|n\.\s?v\.?|nv|s\.\s?a\.?|sas"
    r"|s\.\s?r\.\s?l\.?|srl|sdn\.?\s*bhd\.?|fz-?llc|fz-?co|fze|dmcc|wll|w\.l\.l\.?|k\.k\.)\s*$",
    re.IGNORECASE,
)
# Posting placeholders for a company that is not named ("Confidential", a recruiter's client): no name at all.
_NO_COMPANY = re.compile(
    r"(?i)^(?:|confidential(?:\s+company)?|company\s+(?:name\s+)?confidential|undisclosed(?:\s+company)?"
    r"|not\s+disclosed|hidden|anonymous|private(?:\s+company)?|n/?a|unknown|our\s+client|client(?:\s+company)?"
    r"|(?:an?\s+)?(?:leading|reputed|well[- ]known|fast[- ]growing|established)\s+(?:[\w&-]+\s+){0,4}"
    r"(?:company|firm|organi[sz]ation|group|client|mnc|brand|business|startup)"
    r"|stealth(?:\s+startup)?)$"
)
ACRONYM_CHARS = 3  # VY, AI, IBM stay in capitals
SHOUTED_CHARS = 4  # a name in capitals longer than this is calmed (an acronym on its own is left)


def named_company(name: str) -> bool:
    """False when the posting gives no real company name ("", "Confidential", "A leading audit firm")."""
    return _NO_COMPANY.match(name.strip(" .,-|")) is None


def letter_company(name: str) -> str:
    """The company as a person would write it in a greeting: "VY SYSTEMS PRIVATE LIMITED" -> "VY Systems".

    "" when the posting names no company ("Confidential").
    """
    if not named_company(name):
        return ""
    short = name.strip()
    while (trimmed := _LEGAL_SUFFIX.sub("", short).rstrip(" ,")) != short and trimmed:
        short = trimmed
    if short.isupper() and len(short) > SHOUTED_CHARS:
        short = " ".join(w if len(w) <= ACRONYM_CHARS else w.capitalize() for w in short.split())
    return short or name.strip()


def greeting_for(company: str) -> str:
    """The letter's greeting: the company's hiring team, or "Dear Hiring Manager," when none is named."""
    short = letter_company(company)
    return f"Dear {short} hiring team," if short else "Dear Hiring Manager,"


def cover_letter(  # noqa: PLR0913  (all the letter's inputs)
    master: MasterResume,
    answer: CoverAnswer | None,
    resume_text: str,
    *,
    role: str,
    company: str,
    own_skills: tuple[str, ...] = (),
    strict: bool = False,
    job_text: str = "",
) -> Letter:
    """The letter for one job.

    A first try must be wholly true; its problems go back to the AI for one more try. On that ``strict`` try
    the paragraphs that are true and cliché-free are kept when at least two are; only then is the plain letter
    built from the resume used. A plain letter carries the problems, so the caller can ask again.
    """
    sign_off = f"Best regards,\n{master.name}" if master.name else "Best regards,"
    greeting = greeting_for(company)
    plain = plain_letter(master, role, company, job_text)
    if answer is None:
        return Letter(greeting, plain, sign_off, from_ai=False)
    checker = Checker(master, resume_text, own_skills)
    paragraphs = _distinct([_tidy(p) for p in answer.paragraphs if _tidy(p)])
    checked = [(p, checker.facts(p), checker.style(p)) for p in paragraphs]
    facts = [f for _, found, _ in checked for f in found]
    style = [s for _, _, found in checked for s in found]
    if len(paragraphs) < len([p for p in answer.paragraphs if _tidy(p)]):
        style.append("a paragraph was repeated: write each paragraph once")
    if not strict:  # asked again about these once; on the last try a true letter is not thrown away for them
        style += letter_problems(paragraphs, master, job_text, company)
    if strict:
        kept = [p for p, bad_facts, bad_style in checked if not bad_facts and not bad_style]
        if len(kept) >= MIN_LETTER_PARAGRAPHS:
            return Letter(greeting, kept, sign_off, from_ai=True)
        return Letter(greeting, plain, sign_off, from_ai=False, problems=facts + style)
    if paragraphs and not facts:
        return Letter(greeting, paragraphs, sign_off, from_ai=True, problems=style)
    return Letter(greeting, plain, sign_off, from_ai=False, problems=facts + style)


MIN_LETTER_WORDS = 150
LETTER_ECHO_WORDS = 6  # a letter may say "non-invasive ventilation and high-flow oxygen"; not a sentence
PASTED_WORDS = 12  # this many words in a row from one resume line: a pasted bullet, not a sentence


def _runs(text: str, n: int) -> set[str]:
    words = re.findall(r"[a-z0-9+#%.]+", text.lower())
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def letter_problems(paragraphs: list[str], master: MasterResume, job_text: str, company: str) -> list[str]:
    """What makes a true letter read badly (empty when nothing).

    Too short, pasted resume bullets, the posting's sentences, or a placeholder ("Confidential") named as
    the employer.
    """
    body = "\n".join(paragraphs)
    problems: list[str] = []
    words = len(body.split())
    if words < MIN_LETTER_WORDS:
        problems.append(f"the letter is {words} words: write 180-250 words in 3 paragraphs")
    runs = _runs(body, PASTED_WORDS)
    lines = [b for job in master.experience for b in job.bullets] + [master.summary]
    pasted = [line for line in lines if line and runs & _runs(line, PASTED_WORDS)]
    problems += [
        f'pastes the resume line "{_short_quote(line)}": say what it shows in a sentence of your own'
        for line in pasted[:3]
    ]
    if job_text:
        echo = echo_problems(body, job_text, LETTER_ECHO_WORDS)
        problems += [f"{p}: say it in your own words" for p in echo]
    if (
        not named_company(company)
        and company.strip()
        and re.search(rf"(?i)\b{re.escape(company.strip())}\b", body)
    ):
        problems.append(f'names "{company.strip()}" as the employer: the posting names no company')
    return problems


def _short_quote(text: str, words: int = 8) -> str:
    cut = text.split()
    return " ".join(cut[:words]) + ("..." if len(cut) > words else "")


def _distinct(paragraphs: list[str]) -> list[str]:
    """Each paragraph once: a model sometimes repeats one (seen: the opening twice, the middle missing)."""
    seen: set[str] = set()
    kept = []
    for paragraph in paragraphs:
        key = " ".join(paragraph.lower().split())[:120]
        if key not in seen:
            seen.add(key)
            kept.append(paragraph)
    return kept


def without_claims(letter: Letter, claims: list[str]) -> Letter | None:
    """The letter without the paragraphs the reviewer's claims quote.

    None when fewer than two paragraphs would remain, or when a claim cannot be found in any paragraph (it
    cannot be removed, so the letter cannot be trusted).
    """
    quotes = [c.split(" - ")[0].strip(" \"'“”").lower()[:40] for c in claims]
    if not all(q and any(q in p.lower() for p in letter.paragraphs) for q in quotes):
        return None
    kept = [p for p in letter.paragraphs if not any(q in p.lower() for q in quotes)]
    if len(kept) < MIN_LETTER_PARAGRAPHS:
        return None
    return Letter(letter.greeting, kept, letter.sign_off, from_ai=True)
