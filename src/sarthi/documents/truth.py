# Fact checks ported from OpenApply (MIT), src/truth.ts (truthCheck) @ db7796e. Copyright (c) OpenApply
# contributors. Style lists from ApplyPilot (AGPL-3.0), scoring/validator.py (BANNED_WORDS, LLM_LEAK_PHRASES).
# Modifications (c) Sarthi contributors: Python; works on single texts so each tailored line can be checked
# and replaced on its own.
"""Nothing in a tailored document may be new: numbers, skills, employers and phrases are checked in code.

A number is "known" only when the person's own resume states it ("$300K+" and "300k" are the same claim);
numbers from the job posting are not the person's facts. A skill is known when the resume or the person's
skills name it. Style problems (clichés, an AI
talking about itself, em dashes) are reported separately so they can be fixed by asking again.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sarthi.jobs.skills import skills_in
from sarthi.profile.master import Contact, link_key

_FIGURE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s?(?:%|[kmb]\b|x\b)?\+?", re.I)

BANNED_PHRASES: tuple[str, ...] = (
    "passionate", "committed to", "utilizing", "utilize", "harnessing", "spearheaded", "spearhead",
    "orchestrated", "championed", "pioneered", "robust", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability", "team player", "fast learner",
    "self-starter", "go-getter", "synergy", "holistic", "transformative", "innovative solutions", "paradigm",
    "detail-oriented", "highly motivated", "results-driven", "seamless", "deep understanding",
    "extensive experience", "thrives in", "excels at", "adept at", "well-versed in", "i am confident",
    "i believe", "i am excited", "excited to apply", "thrilled", "instrumental in", "eager to", "leverage",
    "leveraging", "leveraged", "furthermore", "additionally", "moreover", "this demonstrates",
    # career-ops (MIT), modes/pdf.md and modes/cover.md: size words and consultant talk
    "significantly", "dramatically", "stakeholder alignment", "data-driven", "actionable insights",
    "move the needle", "perfect fit", "strong track record", "game-changing", "world-class",
    # hedging that undersells the candidate (seen in real letters)
    "the closest thing i have", "although i have not", "i hope to",
)  # fmt: skip
_BANNED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("align with", r"\balign(?:s|ed|ing)?\s+with\b"),
    ("demonstrate", r"\bdemonstrat\w*\b"),
    ("resonate", r"\bresonat\w*\b"),
)
LEAK_PHRASES: tuple[str, ...] = (
    "i am sorry", "i apologize", "here is the", "here's the", "below is the",
    "as requested", "i have rewritten", "i have updated", "i have removed", "note:", "disclaimer:",
    "tailored resume", "[company]", "[company name]", "[your name]", "[name]", "{company}", "lorem ipsum",
)  # fmt: skip
# An AI talking about itself ("As an AI language model, I..."), not an AI engineer: "as an AI Engineer" and
# "built apps on large language models" are what an AI engineer's own resume says.
_AI_SELF = re.compile(
    r"\b(?:as an ai|i am an ai|i'm an ai|as an? (?:large )?language model|i am a (?:large )?language model)\b"
    r"(?!\s*(?:/|-|&|and\b)?\s*(?:engineer|developer|scientist|researcher|specialist|architect|intern|lead|"
    r"product|consultant|practitioner|ml\b|team|role|position))"
)


def figure_key(figure: str) -> str:
    """The comparable form of a number: "$300K+" -> "300k"."""
    return re.sub(r"[\s,$+]", "", figure.lower())


def figures(text: str) -> list[str]:
    """Every number-like claim in ``text`` as comparable keys."""
    return [k for k in (figure_key(m) for m in _FIGURE.findall(text or "")) if k]


def new_numbers(text: str, known_text: str) -> list[str]:
    """Numbers in ``text`` that ``known_text`` never states (the written form, for messages)."""
    known = set(figures(known_text))
    return [raw.strip() for raw in _FIGURE.findall(text or "") if figure_key(raw) not in known]


def new_skills(text: str, known_skills: Iterable[str], known_text: str) -> list[str]:
    """Skills ``text`` names that neither the person's skills nor ``known_text`` mention."""
    known = list(known_skills)
    already = set(skills_in(known_text, known))
    return [skill for skill in skills_in(text, known) if skill not in already]


def style_problems(text: str) -> list[str]:
    """Clichés, an AI talking about itself, placeholders and em dashes."""
    lower = (text or "").lower()
    problems = [f'uses "{p}"' for p in BANNED_PHRASES if re.search(rf"\b{re.escape(p)}\b", lower)]
    problems += [f'uses "{label}"' for label, pattern in _BANNED_PATTERNS if re.search(pattern, lower)]
    problems += [f'contains "{p}"' for p in LEAK_PHRASES if p in lower]
    if found := _AI_SELF.search(lower):
        problems.append(f'contains "{found.group(0)}"')
    if "—" in (text or ""):
        problems.append("uses an em dash")
    return problems


ECHO_WORDS = 5
_WORDS = re.compile(r"[a-z0-9+#.]+")


def echo_problems(text: str, job_text: str) -> list[str]:
    """Runs of five or more words copied from the job posting (ai-job-search's "echo trap").

    A resume line that repeats the posting's sentences reads as pasted. Hard keywords ("Python",
    "retrieval-augmented generation") are shorter than five words and stay allowed.
    """
    if not job_text:
        return []
    posting = " " + " ".join(_WORDS.findall(job_text.lower())) + " "
    words = _WORDS.findall((text or "").lower())
    for i in range(len(words) - ECHO_WORDS + 1):
        run = " ".join(words[i : i + ECHO_WORDS])
        if f" {run} " in posting:
            return [f'copies the job posting ("{run}")']
    return []


# Verbs that claim leading or owning the work. A re-worded line may keep one the resume used, never add one:
# "used Kafka" must not become "led the Kafka migration" (a level the person did not hold).
_LEAD_VERBS = frozenset(
    {
        "led", "lead", "leading", "headed", "directed", "managed", "owned", "architected", "drove", "founded",
        "mentored", "supervised", "oversaw", "established", "launched", "spearheaded", "championed",
    }
)  # fmt: skip


def verb_upgrade(new: str, old: str) -> list[str]:
    """Leadership verbs in ``new`` that ``old`` never used (the claim moved up a level)."""
    before = set(_WORDS.findall((old or "").lower()))
    after = _WORDS.findall((new or "").lower())
    added = [w for w in dict.fromkeys(after) if w in _LEAD_VERBS and w not in before]
    return [f'claims "{w}", which the original line does not' for w in added]


_IRREGULAR = frozenset(
    {
        "built", "wrote", "ran", "led", "cut", "made", "drove", "grew", "set", "won", "took", "gave",
        "brought", "sped", "rebuilt", "rewrote", "taught", "found", "kept", "began", "chose", "held",
        "met", "sold", "spent", "split", "put", "read", "dug", "upheld", "oversaw", "undertook",
    }
)  # fmt: skip


def _active_verb(word: str) -> bool:
    return word in _IRREGULAR or (word.endswith("ed") and len(word) > 3)  # noqa: PLR2004


def opening_problems(new: str, old: str) -> list[str]:
    """A line that began with an active verb must still do: "Search service built..." is passive."""
    old_words, new_words = _WORDS.findall((old or "").lower()), _WORDS.findall((new or "").lower())
    if old_words and new_words and _active_verb(old_words[0]) and not _active_verb(new_words[0]):
        return [f'starts with "{new_words[0]}": start with an active past-tense verb like the original']
    return []


_TITLE = re.compile(
    r"\b(?:[A-Z][\w+/.-]*\s){0,3}(?:Engineer|Developer|Scientist|Analyst|Architect|Manager|Lead|Designer|"
    r"Consultant|Specialist|Researcher)s?\b"
)


def title_problems(text: str, known_text: str) -> list[str]:
    """Job titles in ``text`` that the resume never gives the person (calling them by the job's title)."""
    known = re.sub(r"\s+", " ", known_text).lower()
    return [
        f'calls the candidate "{title}", a title the resume does not give them'
        for title in dict.fromkeys(m.strip() for m in _TITLE.findall(text or ""))
        if title.lower() not in known
    ]


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def contact_problems(expected: Contact, text: str, links: Iterable[str]) -> list[str]:
    """Contact details from the original resume that a document drops (empty when it keeps them all).

    ``text`` is the document's readable text and ``links`` the targets of its clickable links. The email
    and phone must be readable; a profile link must be clickable or printed, so nothing an employer would
    use to reach the person, or to look them up, is lost when a resume is tailored.
    """
    seen = re.sub(r"\s+", " ", text).lower()
    targets = {link_key(link.removeprefix("mailto:")) for link in links}
    problems: list[str] = []
    if expected.email and expected.email.lower() not in seen and link_key(expected.email) not in targets:
        problems.append(f"email {expected.email}")
    phone = _digits(expected.phone)
    if phone and phone[-10:] not in _digits(text):
        problems.append(f"phone {expected.phone}")
    if expected.location and expected.location.lower() not in seen:
        problems.append(f"location {expected.location}")
    for link in expected.links:
        key = link_key(link)
        if key not in targets and key not in seen.replace("https://", "").replace("http://", "").replace(
            "www.", ""
        ):
            problems.append(f"link {link}")
    return problems


def fact_problems(text: str, known_text: str, known_skills: Iterable[str]) -> list[str]:
    """New numbers or skills in ``text``, compared with what the person's resume (and the job) say."""
    problems = [f'new number "{n}"' for n in new_numbers(text, known_text)]
    problems += [f'new skill "{s}"' for s in new_skills(text, known_skills, known_text)]
    return problems
