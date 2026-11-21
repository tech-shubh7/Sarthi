"""Resume and cover letter PDFs, and a check that an applicant tracking system can read them.

PDFs are made with Typst (typst-py, Apache-2.0), a self-contained typesetter: no browser, no LaTeX. Content is
passed as JSON through ``sys.inputs`` and shown as text by the templates, so nothing in a resume can run as
template code. After rendering, the PDF's own text is extracted (as a tracking system would) and checked for
the name, email, section headings and every employer, title and school.
"""

from __future__ import annotations

import io
import json
import re
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

import typst
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from sarthi.documents.build import Letter
from sarthi.profile.master import MasterResume

MAX_RESUME_PAGES = 2
LETTER_PAPER_COUNTRIES = frozenset({"US", "CA", "MX", "PH"})  # the rest of the world uses A4
_TEMPLATES = resources.files("sarthi.documents") / "templates"


class RenderError(RuntimeError):
    """Typst could not make the PDF."""


def paper_for(country: str) -> str:
    """Typst's paper name for the user's home country."""
    return "us-letter" if country in LETTER_PAPER_COUNTRIES else "a4"


def contact_line(master: MasterResume) -> list[str]:
    """Email, phone, place and links, in that order, skipping blanks."""
    contact = master.contact
    return [v for v in (contact.email, contact.phone, contact.location, *contact.links) if v]


def resume_data(master: MasterResume, paper: str) -> dict[str, Any]:
    """The template's input: the resume as plain JSON values."""
    data = master.model_dump(mode="json", exclude={"source_hash", "contact"})
    data |= {"paper": paper, "contact": contact_line(master), "name": master.name or "Resume"}
    return data


def _compile(template: str, data: dict[str, Any]) -> bytes:
    source = (_TEMPLATES / template).read_bytes()
    try:
        result = typst.compile(source, sys_inputs={"data": json.dumps(data, ensure_ascii=False)})
    except (typst.TypstError, RuntimeError, ValueError, OSError) as error:
        raise RenderError(f"{template}: {str(error)[:300]}") from error
    if not isinstance(result, bytes):
        raise RenderError(f"{template}: Typst returned no PDF")
    return result


def resume_pdf(master: MasterResume, paper: str = "a4") -> bytes:
    """The resume as PDF bytes."""
    return _compile("resume.typ", resume_data(master, paper))


def letter_pdf(master: MasterResume, letter: Letter, paper: str = "a4", today: date | None = None) -> bytes:
    """The cover letter as PDF bytes."""
    data = {
        "paper": paper,
        "name": master.name or "",
        "contact": contact_line(master),
        "date": (today or date.today()).strftime("%d %B %Y"),  # noqa: DTZ011  (the user's local date)
        "greeting": letter.greeting,
        "paragraphs": letter.paragraphs,
        "sign_off": letter.sign_off,
    }
    return _compile("letter.typ", data)


# ---- what a tracking system will read ------------------------------------------------------------------


def pdf_text(pdf: bytes) -> tuple[str, int]:
    """The PDF's extracted text and page count."""
    try:
        reader = PdfReader(io.BytesIO(pdf))
        return "\n".join(page.extract_text() or "" for page in reader.pages), len(reader.pages)
    except PdfReadError:
        return "", 0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def ats_problems(pdf: bytes, master: MasterResume) -> list[str]:
    """What a tracking system could not read back from the resume PDF (empty when all is readable)."""
    text, pages = pdf_text(pdf)
    seen = _norm(text)
    problems: list[str] = []
    if not seen:
        return ["the PDF has no readable text"]
    if pages > MAX_RESUME_PAGES:
        problems.append(f"{pages} pages (recruiters expect at most {MAX_RESUME_PAGES})")
    expected = [("name", master.name), ("email", master.contact.email)]
    sections = (("experience", master.experience), ("skills", master.skills), ("education", master.education))
    expected += [("section", heading) for heading, present in sections if present]
    for job in master.experience:
        expected += [("employer", job.company), ("title", job.title)]
    expected += [("school", s.school) for s in master.education]
    for kind, value in expected:
        if value and _norm(value) not in seen:
            problems.append(f"{kind} “{value}” cannot be read back from the PDF")
    return problems


def write(path: Path, content: bytes) -> None:
    """Write atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)
