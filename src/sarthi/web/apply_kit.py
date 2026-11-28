"""The quick-apply kit: everything for applying by hand on a site Sarthi cannot fill, on one page.

For jobs on sites whose firewall refuses an automated browser (or that need an account Sarthi will not make):
the job opens in the person's own browser, and the page holds what the form will ask for, ready to copy,
next to the tailored resume and letter. Read-only: nothing here changes the database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from sarthi.db.answers import AnswerStore
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.profile.master import load_master, master_path
from sarthi.profile.models import Profile

MAX_ANSWERS = 40  # saved answers shown (the most a form asks, with room to spare)
MAX_LETTER_CHARS = 6000


@dataclass(frozen=True, slots=True)
class KitField:
    """One thing a form asks, with the person's answer."""

    label: str
    value: str


@dataclass
class Kit:
    """The quick-apply kit for one job."""

    job_id: str
    title: str
    company: str
    url: str
    resume: bool = False
    letter: bool = False
    letter_text: str = ""
    details: list[KitField] = field(default_factory=list)
    answers: list[KitField] = field(default_factory=list)


def _letter_text(data_dir: Path | None, letter_file: str) -> str:
    """The letter as text (written next to its PDF), only from inside Sarthi's own data folder."""
    if data_dir is None or not letter_file:
        return ""
    base = data_dir.resolve()
    path = (base / letter_file).resolve().parent / "cover_letter.txt"
    if base not in path.parents or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:MAX_LETTER_CHARS]
    except OSError:
        return ""


def _details(data_dir: Path | None, profile: Profile | None) -> list[KitField]:
    """Name, contact, links and the preferences forms ask for, from the resume Sarthi read."""
    master = load_master(master_path(data_dir)) if data_dir is not None else None
    found: list[KitField] = []
    if master is not None:
        contact = master.contact
        found += [
            KitField("Full name", master.name),
            KitField("Email", contact.email),
            KitField("Phone", contact.phone),
            KitField("Location", contact.location),
        ]
        found += [KitField(_link_label(link), link) for link in contact.links]
    if profile is not None:
        prefs = profile.preferences
        if prefs.years_experience is not None:
            found.append(KitField("Years of experience", f"{prefs.years_experience:g}"))
        if prefs.min_salary is not None:
            found.append(KitField("Expected salary (your minimum)", str(prefs.min_salary)))
    return [f for f in found if f.value]


def _link_label(link: str) -> str:
    for name in ("linkedin", "github", "gitlab", "behance", "dribbble", "leetcode"):
        if name in link.lower():
            return name.capitalize() if name != "linkedin" else "LinkedIn"
    return "Portfolio / website"


def apply_kit(
    conn: sqlite3.Connection, data_dir: Path | None, job_id: str, profile: Profile | None
) -> Kit | None:
    """The kit for ``job_id``, or None for an unknown job."""
    job = JobStore(conn).get(job_id)
    if job is None:
        return None
    kit = Kit(job_id, job.title, job.company, str(job.apply_url or job.url))
    record = DocumentStore(conn).get(job_id)
    if record is not None:
        kit.resume, kit.letter = bool(record.resume_file), bool(record.letter_file)
        kit.letter_text = _letter_text(data_dir, record.letter_file)
    kit.details = _details(data_dir, profile)
    saved = AnswerStore(conn).saved()
    kit.answers = [KitField(q, a) for q, a in list(saved.items())[:MAX_ANSWERS] if a]
    return kit
