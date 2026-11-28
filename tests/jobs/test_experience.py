"""Experience questions are answered from the profile, honestly, and never across subjects."""

import pytest

from sarthi.jobs.experience import experience_answer, has_skill, same_subject, subject, years_option
from sarthi.jobs.forms import Applicant, FormContext, FormField, lookup, suggest

SKILLS = ("Python", "Node.js", "REST APIs", "PostgreSQL", "Git")


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("How many years of work experience do you have with Python (Programming Language)?", "python"),
        ("How many years of Engineering experience do you currently have?", ""),
        ("How many years of work experience do you have?", ""),
        ("Years of experience", ""),
        ("How many years of Node.js experience do you have?", "node js"),
        ("How many years have you worked with Java?", "java"),
        ("What is your level of proficiency in English?", "english"),
        ("Total years of professional experience", ""),
        ("What is your notice period?", None),
        ("First name", None),
    ],
)
def test_subject(label: str, expected: str | None) -> None:
    assert subject(label) == expected


def test_symbol_skills_are_told_apart() -> None:
    assert not same_subject("Years of experience with C++?", "Years of experience with C#?")
    assert subject("Years of experience with .NET?") == "dotnet"


def test_answers_for_different_skills_are_never_shared() -> None:
    py = "How many years of work experience do you have with Python (Programming Language)?"
    java = "How many years of work experience do you have with Java?"
    assert not same_subject(py, java)
    assert lookup({py: "2"}, java) is None
    assert lookup({py: "2"}, py) == (py, "2")
    assert lookup({"What is your level of proficiency in English?": "Professional"},
                  "What is your level of proficiency in German?") is None  # fmt: skip


@pytest.mark.parametrize(
    ("label", "options", "expected"),
    [
        ("How many years of work experience do you have with Python (Programming Language)?", (), "2"),
        ("How many years of Engineering experience do you currently have?", (), "2"),
        ("How many years of work experience do you have with Java?", (), None),  # not on the resume
        ("How many years of Node.js experience do you have?", (), "2"),
        ("Do you have at least 3 years of experience with Python?", ("Yes", "No"), False),
        ("Do you have at least 2 years of experience with Python?", ("Yes", "No"), True),
        ("Do you have 1+ years of experience in PostgreSQL?", ("Yes", "No"), True),
        ("Do you have at least 1 year of experience with Kubernetes?", ("Yes", "No"), None),
        ("Years of experience", ("0-1", "1-3", "3-5", "5+"), "1-3"),
        ("Years of experience", ("Less than 1 year", "1-2 years", "3+ years"), "1-2 years"),
        ("What is your level of proficiency in English?", ("None", "Professional"), None),
        ("Do you have experience with Python?", ("Yes", "No"), True),
        ("Do you have experience with Rust?", ("Yes", "No"), None),
        ("Do you have more than 2.5 years of experience with Python?", ("Yes", "No"), False),
        ("How many years of experience do you have with voice AI?", (), None),
    ],
)
def test_experience_answer(label: str, options: tuple[str, ...], expected: str | bool | None) -> None:
    assert experience_answer(label, options, 2.5, SKILLS) == expected


def test_unknown_years_are_never_guessed() -> None:
    assert experience_answer("How many years of work experience do you have?", (), None, SKILLS) is None


def test_a_fresher_says_zero() -> None:
    assert (
        experience_answer("How many years of work experience do you have with Python?", (), 0, SKILLS) == "0"
    )
    assert years_option(("0-1", "1-3"), 0) == "0-1"
    assert years_option(("Fresher", "1-3 years"), 0) == "Fresher"


@pytest.mark.parametrize(
    ("topic", "skills", "expected"),
    [
        ("python", ("Python 3",), True),
        ("node js", ("Node.js",), True),
        ("java", ("JavaScript",), False),  # whole words only
        ("c", ("C++",), False),
        ("sql", ("PostgreSQL",), False),
        ("react", ("React.js",), True),
        ("c++", ("C++",), True),
        ("c#", ("C++",), False),
        ("voice ai", ("AI",), False),  # a skill never stretches to a wider topic
    ],
)
def test_has_skill(topic: str, skills: tuple[str, ...], expected: bool) -> None:
    assert has_skill(topic, skills) is expected


def test_suggest_answers_linkedin_screening_from_the_profile() -> None:
    ctx = FormContext(Applicant(years_experience=1.0, skills=SKILLS))
    field = FormField(
        "s-2", "How many years of work experience do you have with Python (Programming Language)?"
    )
    hit = suggest(field, ctx)
    assert hit is not None and (hit.value, hit.source) == ("1", "profile")
    unknown = FormField("s-3", "How many years of work experience do you have with Go?", required=True)
    assert suggest(unknown, ctx) is None  # goes to the user
