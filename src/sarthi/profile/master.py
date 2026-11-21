"""The master resume as structured data: the single source of truth every tailored document is built from.

Shape follows OpenApply's ``Resume`` type and Resume-Matcher's sections. Contact details are read from the
resume text on the user's computer (``contact_from``) and never come from, or go to, an AI model. Entries the
AI read from the resume are kept only when the resume text really contains them (``grounded``), so a model
cannot add an employer, school or project that is not there.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

Line = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
Bullet = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=600)]

MASTER_FILE = "master_resume.json"
MAX_BULLETS = 12
_GROUNDED_SHARE = 0.6  # most of a bullet's words must be in the resume text


class Contact(BaseModel):
    """How employers reach the user. Read locally from the resume; editable by the user."""

    model_config = ConfigDict(extra="ignore")

    email: Short = ""
    phone: Short = ""
    location: Short = ""
    links: tuple[Short, ...] = ()


class Job(BaseModel):
    """One position held."""

    model_config = ConfigDict(extra="ignore")

    company: Short
    title: Short
    location: Short = ""
    start: Short = ""
    end: Short = ""
    bullets: tuple[Bullet, ...] = Field(default=(), max_length=MAX_BULLETS)


class Project(BaseModel):
    """A project the user built."""

    model_config = ConfigDict(extra="ignore")

    name: Short
    description: Line = ""
    link: Short = ""
    bullets: tuple[Bullet, ...] = Field(default=(), max_length=MAX_BULLETS)


class School(BaseModel):
    """One degree or course of study."""

    model_config = ConfigDict(extra="ignore")

    school: Short
    degree: Short = ""
    start: Short = ""
    end: Short = ""
    details: tuple[Line, ...] = Field(default=(), max_length=8)


class SkillGroup(BaseModel):
    """Skills under one label ("Languages", "Frameworks"); the label may be empty."""

    model_config = ConfigDict(extra="ignore")

    label: Short = ""
    items: tuple[Short, ...] = Field(default=(), max_length=40)


class Extra(BaseModel):
    """Any other section: certifications, awards, languages."""

    model_config = ConfigDict(extra="ignore")

    heading: Short
    items: tuple[Line, ...] = Field(default=(), max_length=12)


class MasterResume(BaseModel):
    """Everything on the user's resume, in sections."""

    model_config = ConfigDict(extra="ignore")

    name: Short = ""
    headline: Short = ""
    contact: Contact = Contact()
    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1500)] = ""
    experience: tuple[Job, ...] = Field(default=(), max_length=20)
    projects: tuple[Project, ...] = Field(default=(), max_length=15)
    education: tuple[School, ...] = Field(default=(), max_length=8)
    skills: tuple[SkillGroup, ...] = Field(default=(), max_length=12)
    extras: tuple[Extra, ...] = Field(default=(), max_length=8)
    source_hash: str = ""  # hash of the resume text this was read from

    def all_skills(self) -> list[str]:
        """Every skill item, in order, without duplicates."""
        return list(dict.fromkeys(item for group in self.skills for item in group.items))


# ---- contact details, read locally ---------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,16}\d(?![\w.])")
_LINK = re.compile(
    r"(?i)\b(?:https?://)?(?:www\.)?(?:linkedin\.com/in/|github\.com/|gitlab\.com/)[\w./-]+|\bhttps?://[^\s)>\]]+"
)
_NAME_LINE = re.compile(r"^[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){1,3}$")
_NOT_A_NAME = re.compile(r"(?i)\b(?:resume|curriculum|vitae|cv|profile|engineer|developer|contact)\b")
_MIN_PHONE_DIGITS = 10
_MAX_PHONE_DIGITS = 15


def contact_from(text: str) -> Contact:
    """Email, phone and profile links found in the resume text (first of each kind)."""
    email = next(iter(_EMAIL.findall(text)), "")
    phone = ""
    for candidate in _PHONE.findall(text):
        digits = re.sub(r"\D", "", candidate)
        groups = re.findall(r"\d+", candidate)
        only_years = all(len(g) == 4 and 1950 <= int(g) <= 2099 for g in groups)  # noqa: PLR2004  ("2020 - 2024")
        if _MIN_PHONE_DIGITS <= len(digits) <= _MAX_PHONE_DIGITS and not only_years:
            phone = candidate.strip()
            break
    links = []
    for found in _LINK.findall(text):
        link = found.rstrip(".,;")
        if "@" not in link and link not in links:
            links.append(link)
    return Contact(email=email, phone=phone, links=tuple(links[:4]))


def name_from(text: str) -> str:
    """The person's name: the first short line of two to four words that is not a heading. "" when unsure."""
    for raw in text.splitlines()[:6]:
        line = raw.strip()
        if _NAME_LINE.match(line) and not _NOT_A_NAME.search(line):
            return line.title() if line.isupper() else line
    return ""


# ---- keeping only what the resume really says ----------------------------------------------------------

_WORD = re.compile(r"[\w+#.]+")


def _words(text: str) -> list[str]:
    return [w.strip(".").lower() for w in _WORD.findall(text) if w.strip(".")]


def _in_text(value: str, text_words: set[str]) -> bool:
    words = _words(value)
    return bool(words) and all(w in text_words for w in words)


def _mostly_in_text(value: str, text_words: set[str]) -> bool:
    words = [w for w in _words(value) if len(w) > 2]  # noqa: PLR2004
    return not words or sum(w in text_words for w in words) / len(words) >= _GROUNDED_SHARE


def _named_in_text(value: str, text_words: set[str]) -> bool:
    """A name or title the resume contains, allowing small rewording ("Pvt Ltd", "Sr." for "Senior")."""
    return bool(_words(value)) and _mostly_in_text(value, text_words)


_YEAR = re.compile(r"(?:19|20)\d{2}")
_PRESENT = re.compile(r"(?i)^(?:present|current|now|today|ongoing)$")


def _date_in_text(value: str, resume_text: str) -> str:
    """The date when its year (or "present") is in the resume text, else "" (never an invented date)."""
    value = value.strip()
    if not value or _PRESENT.match(value):
        return value
    years = _YEAR.findall(value)
    return value if years and all(year in resume_text for year in years) else ""


def grounded(draft: MasterResume, resume_text: str) -> MasterResume:
    """``draft`` without entries, bullets or skills the resume text does not contain."""
    text_words = set(_words(resume_text))
    experience = tuple(
        job.model_copy(
            update={
                "bullets": tuple(b for b in job.bullets if _mostly_in_text(b, text_words)),
                "start": _date_in_text(job.start, resume_text),
                "end": _date_in_text(job.end, resume_text),
            }
        )
        for job in draft.experience
        if _named_in_text(job.company, text_words) and _named_in_text(job.title, text_words)
    )
    projects = tuple(
        p.model_copy(update={"bullets": tuple(b for b in p.bullets if _mostly_in_text(b, text_words))})
        for p in draft.projects
        if _named_in_text(p.name, text_words)
    )
    education = tuple(
        s.model_copy(
            update={"start": _date_in_text(s.start, resume_text), "end": _date_in_text(s.end, resume_text)}
        )
        for s in draft.education
        if _named_in_text(s.school, text_words)
    )
    skills = tuple(
        g.model_copy(update={"items": tuple(i for i in g.items if _in_text(i, text_words))})
        for g in draft.skills
    )
    return draft.model_copy(
        update={
            "name": draft.name if _in_text(draft.name, text_words) else "",
            "summary": draft.summary if _mostly_in_text(draft.summary, text_words) else "",
            "experience": experience,
            "projects": projects,
            "education": education,
            "skills": tuple(g for g in skills if g.items),
            "extras": tuple(
                e.model_copy(update={"items": tuple(i for i in e.items if _mostly_in_text(i, text_words))})
                for e in draft.extras
            ),
        }
    )


# ---- storage ------------------------------------------------------------------------------------------


def master_path(data_dir: Path) -> Path:
    """Where the master resume is kept (user data, never committed)."""
    return data_dir / MASTER_FILE


def load_master(path: Path) -> MasterResume | None:
    """The saved master resume, or None when missing or unreadable."""
    try:
        return MasterResume.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        return None


def save_master(path: Path, master: MasterResume) -> None:
    """Write atomically (a crash never leaves half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(master.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
