"""Tailoring as checked changes, and the resume page printed by Chromium.

People, companies and jobs are fictional.
"""

import re

import pytest

from sarthi.ai.documents import ResumeChange, change_sections
from sarthi.documents.diffs import apply_changes, dropped, line_problems, term_hints
from sarthi.documents.html_pdf import HtmlPdfError, Page
from sarthi.documents.html_resume import link_label, page_like, resume_html, resume_pdf_html
from sarthi.documents.inplace import contacts_kept
from sarthi.documents.render import pdf_text, resume_pdf
from sarthi.profile.master import Contact, Extra, Job, MasterResume, Project, School, SkillGroup, link_key
from sarthi.profile.resume import pdf_links

FINTECH = "Worked on a fintech application in Node.js covering payments, KYC checks and payouts."
CHATBOT = "Built an HR chatbot used by 900 employees, cutting cost per question by 60%."
RETRIEVAL = "Built LLM workflows with retrieval over policy documents for 40 client firms."
JOB = "We need a Python backend engineer for agentic AI and RAG pipelines. Node.js is a plus."


def person() -> MasterResume:
    return MasterResume(
        name="Meera Iyer",
        headline="Backend & AI Engineer",
        contact=Contact(
            email="meera.iyer@example.com",
            phone="+91 90000 11111",
            location="Kochi, India",
            links=(
                "https://www.linkedin.com/in/meera-example",
                "https://github.com/meera-example",
                "meera.dev",
            ),
        ),
        summary="Backend engineer building AI features and Node.js services for HR and fintech products.",
        experience=(
            Job(
                company="Northwind Apps",
                title="Software Developer",
                location="Kochi, India",
                start="March 2025",
                end="Present",
                bullets=(CHATBOT, RETRIEVAL, FINTECH),
            ),
            Job(
                company="Lakeside Tech",
                title="Developer Intern",
                start="June 2024",
                end="August 2024",
                bullets=("Built a leave tracker in React and Express.js.",),
            ),
        ),
        projects=(
            Project(
                name="ShelfScan",
                description="Python, FastAPI",
                bullets=("Built a FastAPI service that reads book spines from photos.",),
            ),
        ),
        education=(
            School(
                school="Kerala Institute of Computing",
                degree="B.Tech",
                start="2020",
                end="2024",
                details=("CGPA 8.9",),
            ),
        ),
        skills=(
            SkillGroup(label="Languages", items=("Python", "TypeScript", "SQL")),
            SkillGroup(label="AI", items=("LangChain", "RAG", "LLM agents")),
        ),
        extras=(Extra(heading="Certifications", items=("Cloud Basics, Example Academy, 2024",)),),
    )


def change(path: str, value: object, original: object = None, action: str = "replace") -> ResumeChange:
    return ResumeChange(path=path, action=action, original=original, value=value)  # type: ignore[arg-type]


def apply(*changes: ResumeChange):  # type: ignore[no-untyped-def]
    master = person()
    return apply_changes(master, list(changes), job_text=JOB, own_skills=("Python",))


# ---- the per-line truth check ----


def test_a_skill_from_elsewhere_on_the_resume_may_not_enter_a_line() -> None:
    new = "Worked on a fintech application in Python and Node.js covering payments, KYC checks and payouts."
    assert any("Python" in p for p in line_problems(new, FINTECH, ["Python", "Node.js"]))
    result = apply(change("experience[0].bullets[2]", new, FINTECH))
    assert result.resume == person()  # Python is on the skills line, not in this bullet: never added
    assert result.changed == [] and "Python" in result.rejected[0]


@pytest.mark.parametrize(
    ("new", "why"),
    [
        ("Built an HR chatbot used by 1,200 employees, cutting cost per question by 60%.", "1,200"),
        ("Built an HR chatbot used by 900 employees, cutting cost per question.", "drops the number"),
        ("Built an HR chatbot on AWS used by 900 employees, cutting cost per question by 60%.", "AWS"),
        (
            "Built an HR chatbot for Contoso used by 900 employees, cutting cost per question by 60%.",
            "Contoso",
        ),
        ("Led a team building an HR chatbot used by 900 employees, cutting cost by 60%.", "led"),
        ("Built a scalable HR chatbot used by 900 employees, cutting cost per question by 60%.", "scalable"),
    ],
)
def test_a_line_may_not_gain_a_number_tool_company_team_or_scope(new: str, why: str) -> None:
    problems = line_problems(new, CHATBOT, ["Python", "LangChain"])
    assert any(why.lower() in p.lower() for p in problems), problems


def test_a_share_of_the_work_is_not_made_all_of_it() -> None:
    built = FINTECH.replace("Worked on", "Built")
    assert any("worked on" in p for p in line_problems(built, FINTECH))


def test_a_framework_alone_does_not_make_work_agentic() -> None:
    old = "Rebuilt the HR chatbot on LangGraph and moved it to a lighter model."
    new = "Rebuilt the HR chatbot on LangGraph as an agentic workflow and moved it to a lighter model."
    assert any("agentic" in p for p in line_problems(new, old, ["LangGraph"]))
    assert line_problems(new, old + " The bot calls tools as an agent.", ["LangGraph"]) == []


def test_a_reorder_may_quote_bullets_as_re_worded_in_the_same_answer() -> None:
    reworded = "Built a RAG pipeline with an LLM over policy documents for 40 client firms."
    result = apply(
        change("experience[0].bullets", [reworded, CHATBOT, FINTECH], action="reorder"),
        change("experience[0].bullets[1]", reworded, RETRIEVAL),
    )
    assert result.resume.experience[0].bullets == (reworded, CHATBOT, FINTECH)


def test_a_job_word_stuck_on_many_lines_is_kept_to_two() -> None:
    quiz = "Built an AI quiz app in React and Express.js."
    master = person()
    master = master.model_copy(
        update={
            "experience": (master.experience[0], master.experience[1].model_copy(update={"bullets": (quiz,)}))
        }
    )
    summary = master.summary
    changes = [
        change("experience[0].bullets[0]", CHATBOT.replace("HR chatbot", "HR GenAI chatbot"), CHATBOT),
        change("summary", summary.replace("AI features", "GenAI features"), summary),
        change("experience[1].bullets[0]", quiz.replace("AI quiz", "GenAI quiz"), quiz),
    ]
    result = apply_changes(master, changes, job_text=JOB)
    assert len(result.changed) == 2 and "more than 2 lines" in result.rejected[0]


def test_lines_that_already_describe_the_jobs_terms_are_pointed_out() -> None:
    hints = term_hints(person(), JOB).splitlines()
    assert "experience[0].bullets[1]: RAG" in hints  # "LLM workflows with retrieval"
    assert not any(h.startswith("experience[0].bullets[2]") for h in hints)  # the fintech line: no AI words


def test_a_line_may_not_gain_a_result() -> None:
    old = "Built a leave tracker in React and Express.js."
    problems = line_problems("Built a leave tracker in React and Express.js that cut HR emails.", old)
    assert any("result" in p for p in problems)


@pytest.mark.parametrize(
    "new",
    [
        "Built a RAG pipeline with an LLM over policy documents for 40 client firms.",  # the job's word for it
        "Built retrieval-based LLM workflows over policy documents for 40 client firms.",  # its own facts, moved
    ],
)
def test_the_jobs_word_for_what_a_line_already_says_is_allowed(new: str) -> None:
    assert line_problems(new, RETRIEVAL, ["Python", "RAG", "LLM agents"]) == []


def test_a_word_inside_another_word_is_not_its_evidence() -> None:
    old = "Built a JavaScript widget for the dashboard."
    assert any(
        "Java" in p
        for p in line_problems("Built a Java and JavaScript widget for the dashboard.", old, ["Java"])
    )


# ---- applying the changes ----


def test_a_change_whose_quote_does_not_match_is_not_applied() -> None:
    result = apply(
        change(
            "experience[0].bullets[0]",
            "Built an HR chatbot for 900 staff, cutting cost per question by 60%.",
            "Built a chatbot.",
        )
    )
    assert result.resume == person()
    assert "quote" in result.rejected[0]


def test_good_changes_are_applied_and_nothing_is_dropped() -> None:
    reworded = "Built a RAG pipeline with an LLM over policy documents for 40 client firms."
    result = apply(
        change("experience[0].bullets[1]", reworded, RETRIEVAL),
        change("experience[0].bullets", [RETRIEVAL, FINTECH, CHATBOT], action="reorder"),
        change("skills[1].items", ["RAG", "LLM agents"], action="reorder"),  # leaves one out, never dropped
        change(
            "skills[0].items", ["Python", "Go", "SQL", "TypeScript"], action="reorder"
        ),  # adds one, dropped
    )
    resume = result.resume
    assert resume.experience[0].bullets == (reworded, FINTECH, CHATBOT)  # re-ordered by the original lines
    assert resume.skills[1].items == ("RAG", "LLM agents", "LangChain")
    assert resume.skills[0].items == ("Python", "SQL", "TypeScript")
    assert dropped(person(), resume) == []
    assert resume.headline == "Backend & AI Engineer"
    assert {c.where for c in result.changed} == {
        "experience[0].bullets[1]",
        "experience[0].bullets",
        "skills[0].items",
        "skills[1].items",
    }


@pytest.mark.parametrize(
    "bad",
    [
        change("experience[0].bullets", [CHATBOT, FINTECH], action="reorder"),  # one bullet left out
        change("experience[0].bullets", [CHATBOT, FINTECH, "Built a new thing."], action="reorder"),
        change("headline", "Agentic AI Engineer", "Backend & AI Engineer"),  # the headline is never changed
        change("experience[0].title", "Senior Developer", "Software Developer"),
        change("experience[5].bullets[0]", "Built it.", "Built it."),
        change("experience[0].bullets[0]", ["a", "list"], CHATBOT),
    ],
)
def test_changes_that_would_drop_add_or_touch_facts_are_refused(bad: ResumeChange) -> None:
    result = apply(bad)
    assert result.resume == person()
    assert result.rejected


def test_the_summary_keeps_its_facts_and_never_takes_the_jobs_title() -> None:
    old = person().summary
    titled = "Agentic AI Engineer building AI features and Node.js services for HR and fintech products."
    assert apply(change("summary", titled, old)).resume.summary == old
    with_python = "Backend engineer building AI features and Python and Node.js services for HR and fintech."
    assert apply(change("summary", with_python, old)).resume.summary == old
    reordered = "Backend engineer building AI features for HR and fintech products, and Node.js services."
    assert apply(change("summary", reordered, old)).resume.summary == reordered


def test_details_from_another_line_may_not_be_merged_in() -> None:
    old = person().summary
    merged = (
        "Backend engineer building Node.js services for the leave tracker, payroll and approval modules of HR "
        "and fintech products, and AI features."
    )
    result = apply(change("summary", merged, old))
    assert result.resume.summary == old and "details" in result.rejected[0]


def test_style_rules_still_apply() -> None:
    old = "Built a leave tracker in React and Express.js."
    cliche = apply(
        change("experience[1].bullets[0]", "Leveraged React and Express.js to build a leave tracker.", old)
    )
    long = apply(
        change(
            "experience[1].bullets[0]", old[:-1] + " with forms, reports, calendars and approval flows.", old
        )
    )
    copied = apply(
        change("experience[1].bullets[0]", "Built a leave tracker for agentic AI and RAG pipelines.", old)
    )
    assert cliche.resume == long.resume == copied.resume == person()
    dash = apply(
        change("experience[1].bullets[0]", "Built a leave tracker \N{EM DASH} in React, Express.js.", old)
    )
    assert dash.resume.experience[1].bullets == (
        "Built a leave tracker, in React, Express.js.",
    )  # no em dash


def test_the_prompt_names_every_line_by_its_path() -> None:
    text = change_sections(person())
    assert (
        "experience[0].bullets[2]" in text and "skills[1].items" in text and "projects[0].bullets[0]" in text
    )
    assert "meera.iyer@example.com" not in text


# ---- the page ----


def test_the_page_shows_the_persons_headline_links_and_escaped_text() -> None:
    hostile = person().model_copy(update={"summary": "<script>alert(1)</script> Built </section> things."})
    html = resume_html(hostile)
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "Backend &amp; AI Engineer" in html
    assert '<a href="https://www.linkedin.com/in/meera-example">LinkedIn</a>' in html
    assert '<a href="https://meera.dev">Portfolio</a>' in html
    assert [link_label(u) for u in person().contact.links] == ["LinkedIn", "GitHub", "Portfolio"]


async def printed(master: MasterResume, page: Page, pages: int | None = 1) -> bytes:
    try:
        return await resume_pdf_html(master, page, pages)
    except HtmlPdfError as error:  # CI installs no browser
        if "Executable doesn't exist" in str(error) or "playwright install" in str(error):
            pytest.skip(f"Chromium is not installed: {error}")
        raise


async def test_the_resume_prints_on_one_page_with_clickable_links_and_clean_text() -> None:
    pdf = await printed(person(), Page(paper="us-letter", top=28, right=35, bottom=28, left=35))
    text, pages = pdf_text(pdf)
    assert pages == 1
    assert {link_key(u) for u in pdf_links(pdf)} == {
        "linkedin.com/in/meera-example",
        "github.com/meera-example",
        "meera.dev",
    }
    assert contacts_kept(pdf, person().contact) == []
    assert "Backend & AI Engineer" in text
    assert "March 2025 – Present" in text  # the en dash reads back as itself
    assert "Kerala Institute of Computing" in text
    assert not re.search(r"[-�]", text)  # no private-use or missing glyphs
    for heading in ("SUMMARY", "SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION", "CERTIFICATIONS"):
        assert heading in text


async def test_a_resume_a_little_too_long_is_made_to_fit_its_one_page() -> None:
    long = person()
    bullets = tuple(
        f"Built feature {n} of the HR chatbot used by 900 employees across nine offices." for n in range(34)
    )
    long = long.model_copy(
        update={"experience": (long.experience[0].model_copy(update={"bullets": bullets}),)}
    )
    full = await printed(long, Page(paper="a4"), pages=None)
    fitted = await printed(long, Page(paper="a4"), pages=1)
    assert pdf_text(full)[1] == 2 and pdf_text(fitted)[1] == 1
    assert "feature 33" in pdf_text(fitted)[0]


def test_the_page_follows_the_persons_own_pdf() -> None:
    assert page_like(None, "a4") == (Page(paper="a4"), None)
    assert page_like(b"not a pdf", "a4") == (Page(paper="a4"), None)
    page, pages = page_like(resume_pdf(person(), "us-letter"), "a4")
    assert page.paper == "us-letter" and pages == 1
    assert 18 <= page.left <= 72 and 18 <= page.top <= 72
