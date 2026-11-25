"""Tailoring in the user's own design (PDF, LaTeX, Word), keeping every contact detail. Fictional data only."""

from __future__ import annotations

import io
import stat
from pathlib import Path

import docx
import pytest
import typst

from sarthi.ai.documents import TailorAnswer, TailoredJob, job_keywords, sections_text
from sarthi.documents.build import Edit, edits, length_problem, tailored_resume
from sarthi.documents.docx_edit import edit_docx
from sarthi.documents.inplace import contacts_kept, own_design
from sarthi.documents.pdf_edit import edit_pdf
from sarthi.documents.render import pdf_text
from sarthi.documents.tex import edit_tex
from sarthi.documents.truth import (
    contact_problems,
    echo_problems,
    opening_problems,
    title_problems,
    verb_upgrade,
)
from sarthi.profile.latex import is_latex, latex_to_text
from sarthi.profile.master import Contact, Job, MasterResume, SkillGroup, contact_from, links_from
from sarthi.profile.original import Original, keep_original, load_original
from sarthi.profile.resume import extract_text, pdf_links

BULLET_1 = "Built a hybrid search service in Python and FastAPI over 2M product documents, cutting zero-result queries by 38%."
BULLET_2 = "Wrote an offline evaluation harness with 1,200 labelled queries that the team now runs before every model release."
SUMMARY = (
    "Machine learning engineer with 4 years of experience building search and recommendation systems in "
    "Python, with a focus on retrieval and evaluation."
)
LINKS = (
    "https://www.linkedin.com/in/priya-nair-demo",
    "https://github.com/priyanair-demo",
    "https://priyanair.example.dev",
)
DESIGNED = f"""
#set page(paper: "a4", margin: (x: 1.5cm, y: 1.2cm))
#set text(font: "Liberation Serif", size: 10.5pt)
#let teal = rgb("#20786a")
#let sec(t) = {{ v(0.5em); text(fill: teal, weight: "bold", upper(t)); v(-0.6em); line(length: 100%, stroke: 1pt + teal) }}
#grid(columns: (1fr, auto),
  [#text(size: 22pt, weight: "bold", fill: teal)[Priya Nair] \\ Machine Learning Engineer],
  align(right)[priya.nair\\@example.com · +91 98765 43210 \\ Pune, India \\
    #link("{LINKS[0]}")[LinkedIn] · #link("{LINKS[1]}")[GitHub] · #link("{LINKS[2]}")[Portfolio]])
#sec("Summary")
{SUMMARY}
#sec("Experience")
*Senior ML Engineer*, Kestrel Analytics #h(1fr) Jan 2023 - Present
- {BULLET_1.replace("%", "%")}
- {BULLET_2}
#sec("Skills")
*Languages:* Python, SQL, Go
"""


@pytest.fixture(scope="module")
def designed_pdf() -> bytes:
    pdf = typst.compile(DESIGNED.encode("utf-8"))
    assert isinstance(pdf, bytes)
    return pdf


def contact() -> Contact:
    return Contact(
        email="priya.nair@example.com", phone="+91 98765 43210", location="Pune, India", links=LINKS
    )


NEW_1 = "Built a retrieval service in Python and FastAPI over 2M product documents that cut zero-result queries by 38%."
NEW_SUMMARY = (
    "Machine learning engineer with 4 years of building retrieval and recommendation systems in Python, "
    "from search services to offline evaluation."
)


def test_pdf_lines_are_rewritten_in_place_and_links_survive(designed_pdf: bytes) -> None:
    result = edit_pdf(designed_pdf, [Edit(BULLET_1, NEW_1, "b1"), Edit(SUMMARY, NEW_SUMMARY, "summary")])
    assert result.applied == ["b1", "summary"]
    text, pages = pdf_text(result.pdf)
    flat = " ".join(text.split())
    assert pages == 1
    assert NEW_1 in flat  # readable by a tracking system, plain spaces and hyphens
    assert NEW_SUMMARY in flat
    assert BULLET_1 not in flat
    assert "\xad" not in text
    assert pdf_links(result.pdf) == list(LINKS)
    assert contacts_kept(result.pdf, contact()) == []


def test_pdf_line_that_does_not_fit_keeps_its_words(designed_pdf: bytes) -> None:
    long = (
        BULLET_2 + " It also covers every ranking model, every feature job and every dashboard the team uses."
    )
    result = edit_pdf(
        designed_pdf, [Edit(BULLET_2, long, "b2"), Edit("Not on the page at all.", "x", "gone")]
    )
    assert result.skipped == ["b2", "gone"]
    assert BULLET_2 in " ".join(pdf_text(result.pdf)[0].split())


def test_own_design_for_a_pdf_upload(designed_pdf: bytes) -> None:
    design = own_design(Original("pdf", designed_pdf), [Edit(BULLET_1, NEW_1, "b1")], contact())
    assert design.pdf is not None
    assert design.summary()["own"] is True
    assert design.summary()["changed"] == 1


def test_a_document_that_drops_a_contact_detail_fails(designed_pdf: bytes) -> None:
    text, _ = pdf_text(designed_pdf)
    assert contact_problems(contact(), text, pdf_links(designed_pdf)) == []
    more = contact().model_copy(update={"links": (*LINKS, "https://leetcode.com/u/priya-demo")})
    assert contact_problems(more, text, pdf_links(designed_pdf)) == ["link https://leetcode.com/u/priya-demo"]
    assert contact_problems(contact(), "Priya Nair", []) == [
        "email priya.nair@example.com",
        "phone +91 98765 43210",
        "location Pune, India",
        *(f"link {link}" for link in LINKS),
    ]
    design = own_design(Original("pdf", designed_pdf), [], more)
    assert design.pdf is None
    assert "leetcode" in design.problems[0]


def test_hidden_pdf_links_reach_the_master_contact(designed_pdf: bytes) -> None:
    found = contact_from(extract_text(designed_pdf))
    assert found.email == "priya.nair@example.com"
    assert set(LINKS) <= set(found.links)


@pytest.mark.parametrize(
    ("text", "links"),
    [
        (
            "Asha Rao\nasha.dev | asha@example.com | github.com/asha-demo",
            ["github.com/asha-demo", "asha.dev"],
        ),
        ("Asha Rao\nx.com/asha_demo · leetcode.com/u/asha", ["leetcode.com/u/asha", "x.com/asha_demo"]),
        ("Asha Rao | ASP.NET, B.Tech | asha@example.com", []),  # tech names in a headline are not sites
    ],
)
def test_profile_links_of_every_kind_are_found(text: str, links: list[str]) -> None:
    assert sorted(links_from(text)) == sorted(links)


# ---- LaTeX ----------------------------------------------------------------------------------------------

TEX = r"""
\documentclass[letterpaper,11pt]{article}
\usepackage[hidelinks]{hyperref}
\newcommand{\resumeItem}[1]{\item\small{#1 \vspace{-2pt}}}
\begin{document}
\begin{center}
  \textbf{\Huge Priya Nair} \\ \small +91 98765 43210 $|$ \href{mailto:priya.nair@example.com}{priya.nair@example.com} $|$
  \href{https://www.linkedin.com/in/priya-nair-demo}{\underline{LinkedIn}} $|$
  \href{https://github.com/priyanair-demo}{\underline{GitHub}}
\end{center}
\section{Experience}
\begin{itemize}
  \resumeItem{Built a hybrid search service in \textbf{Python} and FastAPI over 2M product documents, cutting zero-result queries by 38\%.}
  \resumeItem{Wrote an offline evaluation harness with 1,200 labelled queries that the team now runs before every model release.}
\end{itemize}
\section{Skills}
\textbf{Languages}{: Python, SQL, Go} % a comment
\end{document}
"""


def test_latex_upload_is_read_as_its_printed_text() -> None:
    assert is_latex(TEX)
    text = extract_text(TEX.encode("utf-8"))
    assert "\\resumeItem" not in text
    assert BULLET_1 in " ".join(text.split())
    assert "https://github.com/priyanair-demo" in text
    assert "a comment" not in text
    assert latex_to_text(TEX).startswith("Priya Nair")


def test_latex_source_is_edited_in_place() -> None:
    new_skills = "SQL, Python, Go"
    result = edit_tex(
        TEX,
        [
            Edit(BULLET_1, NEW_1 + " & more", "b1"),
            Edit("Python, SQL, Go", new_skills, "skills"),
            Edit("nope", "x", "z"),
        ],
    )
    assert result.applied == ["b1", "skills"]
    assert result.skipped == ["z"]
    assert r"\resumeItem{Built a retrieval service in \textbf{Python} and FastAPI" in result.source
    assert r"38\%. \& more}" in result.source  # escaped, macro kept, bold word kept bold
    assert r"\textbf{Languages}{: SQL, Python, Go}" in result.source
    before, after = (
        TEX.split(r"\section{Experience}", maxsplit=1)[0],
        result.source.split(r"\section{Experience}")[0],
    )
    assert before == after  # the header with its links is untouched, byte for byte


def test_tex_without_an_engine_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sarthi.documents.tex.engine", lambda: None)
    design = own_design(Original("tex", TEX.encode("utf-8")), [Edit(BULLET_1, NEW_1, "b1")], contact())
    assert design.pdf is None
    assert design.source is not None and b"retrieval service" in design.source
    assert "LaTeX" in design.problems[0]


# ---- Word -------------------------------------------------------------------------------------------------


def _word() -> bytes:
    document = docx.Document()
    document.add_paragraph("Priya Nair")
    bullet = document.add_paragraph(style="List Bullet")
    run = bullet.add_run(BULLET_1)
    run.bold = True
    document.add_paragraph("Languages: Python, SQL, Go")
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def test_word_file_is_edited_keeping_styles() -> None:
    result = edit_docx(
        _word(), [Edit(BULLET_1, NEW_1, "b1"), Edit("Python, SQL, Go", "SQL, Python, Go", "skills")]
    )
    assert result.applied == ["b1", "skills"]
    document = docx.Document(io.BytesIO(result.docx))
    paragraphs = document.paragraphs
    assert paragraphs[1].text == NEW_1
    assert paragraphs[1].style.name == "List Bullet"
    assert paragraphs[1].runs[0].bold
    assert paragraphs[2].text == "Languages: SQL, Python, Go"


# ---- the kept upload --------------------------------------------------------------------------------------


def test_original_is_kept_private_and_used_only_for_its_own_text(tmp_path: Path, designed_pdf: bytes) -> None:
    keep_original(tmp_path, designed_pdf, "text A")
    path = tmp_path / "resume" / "original.pdf"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    kept = load_original(tmp_path, "text A")
    assert kept is not None and kept.kind == "pdf" and kept.data == designed_pdf
    assert load_original(tmp_path, "text B") is None  # a newer upload or a cancelled setup
    keep_original(tmp_path, TEX.encode("utf-8"), "text C")
    assert not path.exists()
    assert (tmp_path / "resume" / "original.tex").is_file()
    keep_original(tmp_path, b"Plain text resume with no design at all, long enough.", "text D")
    assert load_original(tmp_path, "text D") is None
    assert not (tmp_path / "resume" / "original.tex").exists()


# ---- wording ----------------------------------------------------------------------------------------------


def _master() -> MasterResume:
    return MasterResume(
        name="Priya Nair",
        summary=SUMMARY,
        contact=contact(),
        experience=(
            Job(company="Kestrel Analytics", title="Senior ML Engineer", bullets=(BULLET_1, BULLET_2)),
        ),
        skills=(SkillGroup(label="Languages", items=("Python", "SQL", "Go")),),
    )


RESUME_TEXT = (
    f"Priya Nair\n{SUMMARY}\nKestrel Analytics Senior ML Engineer\n{BULLET_1}\n{BULLET_2}\nPython, SQL, Go"
)


def test_each_bullet_is_reworded_in_its_own_place() -> None:
    answer = TailorAnswer(
        summary=NEW_SUMMARY,
        experience=[TailoredJob(index=0, bullets=[NEW_1, BULLET_2])],
        skills_first=["SQL"],
    )
    built = tailored_resume(_master(), answer, RESUME_TEXT)
    assert built.resume.experience[0].bullets == (NEW_1, BULLET_2)
    assert [(e.where, e.new) for e in edits(_master(), built.resume)] == [
        ("summary", NEW_SUMMARY),
        ("Kestrel Analytics bullet 1", NEW_1),
        ("skills Languages", "SQL, Python, Go"),
    ]


def test_dropped_bullets_long_lines_and_promotions_are_not_used() -> None:
    led = "Led the team that built a hybrid search service in Python and FastAPI over 2M documents, cutting 38%."
    answer = TailorAnswer(experience=[TailoredJob(index=0, bullets=[led, BULLET_2 + " " + BULLET_2])])
    built = tailored_resume(_master(), answer, RESUME_TEXT, strict=True)
    assert built.resume.experience[0].bullets == (BULLET_1, BULLET_2)
    assert any('claims "led"' in r for r in built.reverted)
    assert any("keep its length" in r for r in built.reverted)
    dropped = tailored_resume(
        _master(), TailorAnswer(experience=[TailoredJob(index=0, bullets=[NEW_1])]), RESUME_TEXT
    )
    assert dropped.resume.experience[0].bullets == (BULLET_1, BULLET_2)
    assert any("give exactly 2" in p for p in dropped.problems)


def test_copying_the_posting_is_caught() -> None:
    job = "You will design and evaluate retrieval systems for millions of shoppers every day."
    copied = "Built search in Python; design and evaluate retrieval systems for millions of shoppers."
    assert echo_problems(copied, job)
    assert not echo_problems("Built retrieval in Python and evaluated it offline.", job)
    answer = TailorAnswer(
        experience=[TailoredJob(index=0, bullets=[copied + " Cut zero-result queries by 38%.", BULLET_2])]
    )
    built = tailored_resume(_master(), answer, RESUME_TEXT, strict=True, job_text=job)
    assert built.resume.experience[0].bullets[0] == BULLET_1


def test_passive_rewrites_and_borrowed_titles_are_caught() -> None:
    assert opening_problems(
        "Hybrid search service built in Python.", "Built a hybrid search service in Python."
    )
    assert not opening_problems("Shipped a retrieval service in Python.", "Built a search service in Python.")
    assert title_problems("AI Engineer with 4 years in Python.", RESUME_TEXT)
    assert not title_problems("Senior ML Engineer with 4 years in Python.", RESUME_TEXT)
    borrowed = (
        "AI Engineer with 4 years of experience building retrieval systems in Python, focused on evaluation."
    )
    built = tailored_resume(_master(), TailorAnswer(summary=borrowed), RESUME_TEXT)
    assert built.resume.summary == SUMMARY


def test_verbs_and_lengths() -> None:
    assert verb_upgrade("Architected the search service", "Built the search service")
    assert not verb_upgrade("Led the migration again", "Led the migration")
    assert length_problem("x" * 130, "y" * 100)
    assert not length_problem("x" * 105, "y" * 100)
    assert length_problem("x" * 40, "y" * 100)


def test_prompt_shows_lengths_and_the_jobs_keywords() -> None:
    text = sections_text(_master(), lengths=True)
    assert f"1. ({len(BULLET_1)} chars) {BULLET_1}" in text
    keywords = job_keywords(_master(), "We need Python, Go and Kubernetes.")
    assert keywords.startswith("have: Python, Go")
    assert "lack: Kubernetes" in keywords
