"""The resume as an HTML page in the person's own kind of layout, printed to PDF by Chromium.

The layout follows a classic one-column Word resume (centred name in spaced capitals, the person's own
headline, a contact line, profile links as words, spaced-capital section headings over a thin rule, role and
date rows, bullets). Text reflows, so a re-worded bullet never has to fit an old line's box, and every link is
a real ``<a href>``. The headline is always the person's own: a tailored resume never takes the job's title.

When the person's uploaded PDF is kept, its paper size and side margins are used and the resume is made to
keep its page count.
"""

from __future__ import annotations

import base64
import importlib
import re
from functools import cache
from importlib import resources
from typing import Any

from jinja2 import Environment, select_autoescape

from sarthi.documents.html_pdf import Page, html_to_pdf
from sarthi.profile.master import MasterResume, link_key

# PyMuPDF ships without type hints: used as an untyped module.
pymupdf: Any = importlib.import_module("pymupdf")

_TEMPLATES = resources.files("sarthi.documents") / "templates"
_FONT = resources.files("sarthi") / "web" / "static" / "fonts" / "InterVariable.woff2"  # bundled, OFL
BODY_PT = 9.5
TITLE_PT = 9.9
DASH = chr(0x2013)  # an en dash between dates, as resumes print them
DOT = f" {chr(0xB7)} "  # a middle dot between company and place

_SITES = (
    ("linkedin.com", "LinkedIn"), ("github.com", "GitHub"), ("gitlab.com", "GitLab"),
    ("leetcode.com", "LeetCode"), ("kaggle.com", "Kaggle"), ("medium.com", "Medium"),
    ("stackoverflow.com", "Stack Overflow"), ("huggingface.co", "Hugging Face"), ("behance.net", "Behance"),
    ("dribbble.com", "Dribbble"), ("x.com", "X"), ("twitter.com", "X"), ("dev.to", "DEV"),
    ("hashnode.dev", "Blog"), ("substack.com", "Blog"),
)  # fmt: skip


def link_label(url: str) -> str:
    """The word a resume prints for a profile link: "LinkedIn", "GitHub"; a personal site is "Portfolio"."""
    host = link_key(url).split("/")[0]
    for domain, label in _SITES:
        if host == domain or host.endswith("." + domain):
            return label
    return "Portfolio"


def _href(url: str) -> str:
    url = url.strip()
    return url if re.match(r"(?i)^https?://", url) else f"https://{url}"


def _dates(start: str, end: str) -> str:
    return f" {DASH} ".join(part for part in (start.strip(), end.strip()) if part)


@cache
def _font_url() -> str:
    try:
        return "data:font/woff2;base64," + base64.b64encode(_FONT.read_bytes()).decode("ascii")
    except OSError:  # an install without the web assets: Chromium falls back to a system sans-serif font
        return "data:,"


@cache
def _environment() -> Environment:
    return Environment(
        autoescape=select_autoescape(default=True, default_for_string=True), keep_trailing_newline=True
    )


def resume_context(master: MasterResume) -> dict[str, Any]:
    """What the template shows: every section of ``master``, nothing added."""
    contact = master.contact
    line: list[dict[str, str]] = [{"text": v} for v in (contact.location, contact.phone) if v]
    if contact.email:
        line.append({"text": contact.email, "href": f"mailto:{contact.email}"})
    labels: list[str] = []
    links = []
    for url in contact.links:
        label = link_label(url)
        label = label if label not in labels else link_key(url)  # a second site of one kind shows its address
        labels.append(label)
        links.append({"text": label, "href": _href(url)})
    return {
        "name": master.name or "Resume",
        "headline": master.headline,
        "contact": line,
        "links": links,
        "summary": master.summary,
        "skills": [{"label": g.label, "names": list(g.items)} for g in master.skills if g.items],
        "experience": [
            {
                "title": job.title,
                "dates": _dates(job.start, job.end),
                "where": DOT.join(p for p in (job.company, job.location) if p),
                "bullets": list(job.bullets),
            }
            for job in master.experience
        ],
        "projects": [
            {
                "name": p.name,
                "description": p.description,
                "link": _href(p.link) if p.link else "",
                "bullets": list(p.bullets),
            }
            for p in master.projects
        ],
        "education": [
            {
                "school": s.school,
                "dates": _dates(s.start, s.end),
                "detail": DOT.join(p for p in (s.degree, *s.details) if p),
            }
            for s in master.education
        ],
        "extras": [{"heading": e.heading, "lines": list(e.items)} for e in master.extras if e.items],
        "font_url": _font_url(),
        "body_pt": BODY_PT,
        "title_pt": TITLE_PT,
    }


def resume_html(master: MasterResume) -> str:
    """The resume as one self-contained HTML page (fonts inline, no scripts, all text escaped)."""
    template = _environment().from_string((_TEMPLATES / "resume.html").read_text(encoding="utf-8"))
    return template.render(**resume_context(master))


LETTER_SIZE = (612.0, 792.0)
_SIZE_SLACK = 20.0
_MIN_MARGIN, _MAX_MARGIN = 18.0, 72.0


def page_like(original_pdf: bytes | None, paper: str) -> tuple[Page, int | None]:
    """The paper, margins and page count of the person's own PDF; ``paper`` and 0.5 inch without one."""
    if not original_pdf:
        return Page(paper=paper), None
    try:
        document = pymupdf.open(stream=original_pdf, filetype="pdf")
    except (RuntimeError, ValueError):
        return Page(paper=paper), None
    with document:
        first = document[0]
        width, height = first.rect.width, first.rect.height
        boxes = [b[:4] for b in first.get_text("blocks") if str(b[4]).strip()]
        pages = len(document)
    letter = abs(width - LETTER_SIZE[0]) < _SIZE_SLACK and abs(height - LETTER_SIZE[1]) < _SIZE_SLACK
    if not boxes:
        return Page(paper="us-letter" if letter else "a4"), pages

    def clamp(value: float) -> float:
        return round(min(max(value, _MIN_MARGIN), _MAX_MARGIN), 1)

    left = clamp(min(b[0] for b in boxes))
    right = clamp(width - max(b[2] for b in boxes))
    top = clamp(min(b[1] for b in boxes))
    return Page(paper="us-letter" if letter else "a4", top=top, right=right, bottom=top, left=left), pages


async def resume_pdf_html(master: MasterResume, page: Page, pages: int | None = 1) -> bytes:
    """The resume PDF, kept to ``pages`` pages when it can be (None: as long as it is)."""
    return await html_to_pdf(resume_html(master), page, fit_pages=pages)
