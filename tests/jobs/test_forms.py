"""Field mapping, ported from OpenApply's test/fieldmap.test.ts (MIT) plus Sarthi's own cases.

Option lists are real ones from live Greenhouse forms. People are fictional.
"""

from dataclasses import replace
from datetime import date

import pytest

from sarthi.jobs.forms import (
    Applicant,
    FieldType,
    FormContext,
    FormField,
    best_place,
    commitment_place,
    pick_options,
    suggest,
    to_date,
    unanswered,
    yes_no_option,
)

AI_EXPERIENCE = (
    "I use AI tools daily as part of my professional job duties for my employer, but don't teach others.",
    "I use AI tools daily as part of my professional job duties for my employer, and I teach others.",
    "I do not use AI at work, but I regularly use it in my personal life. I'd love to learn more.",
    "I use AI tools at work, but only occasionally or weekly. I'd love to learn more.",
    "I do not use AI tools in my work or personal life. I am opening to learning",
)
TANGIBLE = ("Career Growth", "Work-life Balance", "Remote Work", "Leadership", "Compensation", "Benefits", "PTO",
            "Career Stability", "Culture", "Company Outlook")  # fmt: skip
DEGREES = ("Associate's Degree", "Bachelor's Degree", "Doctor of Philosophy (PhD)", "Engineer's Degree",
           "High School Diploma", "Master's Degree", "Other")  # fmt: skip
DISCIPLINES = ("Accounting", "Business", "Chemistry", "Computer Science", "Economics", "Engineering",
               "Information Systems", "Law", "Political Science", "Science", "Statistics & Decision Theory")  # fmt: skip

PERSON = Applicant(first_name="Asha", last_name="Rao", email="asha@example.com", phone="+91 98765 43210",
                   location="Pune, India", country="India")  # fmt: skip


def ctx(applicant: Applicant = PERSON, **kw: object) -> FormContext:
    return FormContext(applicant=applicant, **kw)  # type: ignore[arg-type]


def checkbox(label: str, options: tuple[str, ...]) -> FormField:
    return FormField("q[]", label, "checkbox", True, options)


def ask(form_field: FormField, answer: str, applicant: Applicant = PERSON) -> str | None:
    got = suggest(form_field, ctx(applicant, answers={form_field.label: answer}))
    return got.value if got else None


def field(
    label: str, kind: FieldType = "text", options: tuple[str, ...] = (), required: bool = True
) -> FormField:
    return FormField(label, label, kind, required, options)


# ---- multi-select ---------------------------------------------------------------------------------------


def test_an_answer_equal_to_one_option_picks_exactly_it() -> None:
    f = checkbox("Which statement best describes your current experience with AI?", AI_EXPERIENCE)
    for option in AI_EXPERIENCE[:4]:
        assert ask(f, option) == option
    assert ask(f, AI_EXPERIENCE[1].rstrip(".")) == AI_EXPERIENCE[1]
    assert pick_options(AI_EXPERIENCE, AI_EXPERIENCE[1]) == [AI_EXPERIENCE[1]]


def test_a_list_picks_each_option_even_with_commas_inside() -> None:
    ai = checkbox("AI experience", AI_EXPERIENCE)
    assert ask(ai, f"{AI_EXPERIENCE[1]}, {AI_EXPERIENCE[3]}") == f"{AI_EXPERIENCE[1]}, {AI_EXPERIENCE[3]}"
    assert pick_options(AI_EXPERIENCE, f"{AI_EXPERIENCE[3]} | {AI_EXPERIENCE[0]}") == [
        AI_EXPERIENCE[3],
        AI_EXPERIENCE[0],
    ]
    tangible = checkbox("What tangible factors matter most?", TANGIBLE)
    assert (
        ask(tangible, "Career Growth, Compensation, Remote Work")
        == "Career Growth, Compensation, Remote Work"
    )
    assert ask(tangible, "remote work; PTO") == "Remote Work, PTO"


def test_a_partial_piece_never_picks_a_different_option() -> None:
    f = checkbox("AI experience", AI_EXPERIENCE)
    assert ask(f, "I use AI tools daily as part of my professional job duties for my employer") is None
    assert ask(f, "I use AI tools") is None
    assert ask(f, "I do not use AI at work") == AI_EXPERIENCE[2]
    assert pick_options(("Other", "Python, Go", "Rust"), "I work in Rust and other things") == []
    assert pick_options(("Other", "Python, Go", "Rust"), "Rust and Other") == ["Rust", "Other"]
    assert pick_options(("Yes", "Yes, and I will relocate", "No"), "Yes, I will relocate") == [
        "Yes, and I will relocate"
    ]


# ---- education ------------------------------------------------------------------------------------------


EDUCATED = replace(PERSON, school="Pune University", degree="Bachelor's Degree", discipline="Information Science",
                   end_year="2023")  # fmt: skip


def test_education_comes_from_the_profile_and_lists_need_exact_wording() -> None:
    assert suggest(field("School", "combobox"), ctx(EDUCATED)).value == "Pune University"  # type: ignore[union-attr]
    assert suggest(field("Degree", "combobox", DEGREES), ctx(EDUCATED)).value == "Bachelor's Degree"  # type: ignore[union-attr]
    assert suggest(field("End date year", "number"), ctx(EDUCATED)).value == "2023"  # type: ignore[union-attr]
    assert suggest(field("Start date year", "number"), ctx(EDUCATED)) is None  # nothing invented
    assert (
        suggest(field("Discipline", "combobox", DISCIPLINES), ctx(EDUCATED)) is None
    )  # not "Information Systems"
    systems = replace(EDUCATED, discipline="information systems")
    assert suggest(field("Discipline", "combobox", DISCIPLINES), ctx(systems)).value == "Information Systems"  # type: ignore[union-attr]
    for options, own in ((("Biochemistry", "Physics"), "Chemistry"), (("Econometrics", "Law"), "Economics")):
        assert (
            suggest(field("Discipline", "combobox", options), ctx(replace(EDUCATED, discipline=own))) is None
        )
    assert suggest(field("Why did you leave school?", "textarea"), ctx(EDUCATED)) is None


def test_a_missing_year_never_falls_through_to_the_notice_period() -> None:
    noticed = replace(PERSON, notice_period="1-2 weeks")
    assert suggest(field("Start date year", "number", required=False), ctx(noticed)) is None
    assert suggest(field("Start date"), ctx(noticed)).value == "1-2 weeks"  # type: ignore[union-attr]


# ---- dates ----------------------------------------------------------------------------------------------

WEDNESDAY = date(2026, 10, 21)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("1-2 weeks", "2026-11-04"), ("2 weeks", "2026-11-04"), ("two weeks' notice", "2026-11-04"),
        ("within 2 weeks", "2026-11-04"), ("1 to 2 weeks", "2026-11-04"), ("Immediately", "2026-10-21"),
        ("ASAP", "2026-10-21"), ("3 days", "2026-10-26"), ("a month", "2026-11-20"), ("2026-11-15", "2026-11-15"),
        ("11/15/2026", "2026-11-15"), ("Nov 15, 2026", "2026-11-15"), ("November 15", "2026-11-15"),
        ("Jan 5", "2027-01-05"),
    ],
)  # fmt: skip
def test_to_date(raw: str, want: str) -> None:
    assert to_date(raw, WEDNESDAY) == want


@pytest.mark.parametrize(
    "raw", ["After my notice period", "Flexible", "31/31/2026", "Feb 30, 2027", "", "1-2"]
)
def test_to_date_refuses_non_dates(raw: str) -> None:
    assert to_date(raw, WEDNESDAY) is None


@pytest.mark.parametrize(
    ("raw", "day_first", "want"),
    [
        ("04/11/2026", False, "2026-04-11"),  # the US: month first
        ("04/11/2026", True, "2026-11-04"),  # the UK, India, Germany...: day first
        ("13/01/2026", False, "2026-01-13"),  # 13 cannot be a month, whoever wrote it
        ("24.12.2026", True, "2026-12-24"),
        ("01/25/2026", True, "2026-01-25"),  # 25 cannot be a month
    ],
)
def test_numeric_dates_follow_how_the_user_writes_them(raw: str, day_first: bool, want: str) -> None:
    assert to_date(raw, WEDNESDAY, day_first=day_first) == want


def test_date_fields_never_get_free_text() -> None:
    start = field("When is the earliest you would want to start working with us?", "date")
    assert ask(start, "After my notice period") is None
    assert suggest(start, ctx(replace(PERSON, notice_period="2 weeks"))).value == to_date("2 weeks")  # type: ignore[union-attr]


# ---- relocation, office days, sponsorship ----------------------------------------------------------------

SF_OFFICE = "Are you willing to work from our office in San Francisco, California full-time, 3+ days a week?"
SF_RELOCATE = "If you are not located near San Francisco, California, are you willing to relocate if offered employment?"
NY_ANSWER = "I'm based in New York, so no relocation needed. I prefer hybrid but I'm open to being in person."


def yes_no(label: str) -> FormField:
    return field(label, "select", ("Yes", "No"))


def test_place_commitments_need_an_answer_naming_that_place() -> None:
    ny = replace(PERSON, location="New York, NY", saved_answers={"Willing to relocate": NY_ANSWER})
    willing = replace(ny, willing_to_relocate=True)
    for who in (ny, willing):
        for f in (yes_no(SF_OFFICE), yes_no(SF_RELOCATE), field(SF_RELOCATE)):
            assert suggest(f, ctx(who)) is None, f.label
    assert (
        suggest(field("When can you start in Boston?"), ctx(replace(ny, notice_period="1-2 weeks"))) is None
    )
    sf = replace(ny, saved_answers={"Willing to relocate": "Yes, happy to move to San Francisco"})
    assert suggest(yes_no(SF_RELOCATE), ctx(sf)).value == "Yes"  # type: ignore[union-attr]
    assert suggest(field("Are you willing to relocate to New York?"), ctx(ny)).value == NY_ANSWER  # type: ignore[union-attr]
    assert suggest(yes_no("Are you willing to relocate?"), ctx(willing)).value == "Yes"  # type: ignore[union-attr]
    assert suggest(yes_no("Are you willing to relocate?"), ctx(willing, job_location="Chicago")) is None
    assert suggest(field("Are you willing to relocate?"), ctx(ny, job_location="NYC")).value == NY_ANSWER  # type: ignore[union-attr]
    assert (
        commitment_place(
            "Do you have any experience in mailroom operations, reprographics, imaging services, or other related "
            "office support roles?"
        )
        is None
    )


def test_sponsorship_and_authorisation_follow_the_job_country() -> None:
    abroad = replace(PERSON, needs_sponsorship=True, work_authorized=False)
    sponsor = yes_no("Will you now or in the future require visa sponsorship to work in the United States?")
    authorised = yes_no("Are you legally authorized to work in the United States?")
    assert suggest(sponsor, ctx(abroad)).value == "Yes"  # type: ignore[union-attr]
    assert suggest(authorised, ctx(abroad)).value == "No"  # type: ignore[union-attr]
    assert suggest(sponsor, ctx(PERSON)) is None  # unknown: the user answers, never a guess


# ---- defaults, files, identity ----------------------------------------------------------------------------


def test_demographic_questions_get_decline_or_nothing() -> None:
    gender = field("Gender", "select", ("Male", "Female", "I don't wish to answer"))
    assert suggest(gender, ctx()).value == "I don't wish to answer"  # type: ignore[union-attr]
    assert suggest(field("Veteran status", "select", ("Yes", "No")), ctx()) is None


def test_how_did_you_hear() -> None:
    assert suggest(field("How did you hear about us?"), ctx()).value == "Company website"  # type: ignore[union-attr]
    picked = suggest(
        field("How did you hear about this job?", "select", ("LinkedIn", "Company careers page")), ctx()
    )
    assert picked is not None
    assert picked.value == "Company careers page"


def test_identity_contact_and_files() -> None:
    files = ctx(resume_file="/r.pdf", cover_file="/c.pdf", cover_letter="Dear team")
    assert suggest(field("First Name"), files).value == "Asha"  # type: ignore[union-attr]
    assert suggest(field("Full name"), files).value == "Asha Rao"  # type: ignore[union-attr]
    assert suggest(field("Email"), files).value == "asha@example.com"  # type: ignore[union-attr]
    assert suggest(field("Phone country code"), files).value == "+91"  # type: ignore[union-attr]
    assert suggest(field("Resume/CV", "file"), files).value == "/r.pdf"  # type: ignore[union-attr]
    assert suggest(field("Cover Letter", "file"), files).value == "/c.pdf"  # type: ignore[union-attr]
    assert suggest(field("Cover letter", "textarea"), files).value == "Dear team"  # type: ignore[union-attr]
    assert suggest(field("Portfolio", "file"), files) is None


def test_job_answers_beat_saved_answers() -> None:
    who = replace(PERSON, saved_answers={"Notice period": "60 days"})
    f = field("What is your notice period?")
    assert suggest(f, ctx(who)).value == "60 days"  # type: ignore[union-attr]
    assert suggest(f, ctx(who, answers={f.key: "30 days"})).value == "30 days"  # type: ignore[union-attr]


def test_unanswered_lists_only_required_questions_without_a_safe_answer() -> None:
    fields = [
        field("Email"),
        field("Why do you want to work here?", "textarea"),
        field("Favourite colour", required=False),
        field("Resume", "file"),
    ]
    assert [f.label for f in unanswered(fields, ctx())] == ["Why do you want to work here?"]


# ---- options and places ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "yes", "want"),
    [
        (("Yes", "No"), True, "Yes"),
        (
            ("I am authorized to work", "I am not authorized", "Decline to answer"),
            True,
            "I am authorized to work",
        ),
        (
            ("I will require sponsorship", "No, I will not require sponsorship"),
            False,
            "No, I will not require sponsorship",
        ),
        (("Maybe", "Perhaps"), True, None),
    ],
)
def test_yes_no_option(options: tuple[str, ...], yes: bool, want: str | None) -> None:
    assert yes_no_option(options, yes) == want


def test_best_place_needs_city_and_region() -> None:
    options = ("Brooklyn, New South Wales, Australia", "Brooklyn, New York, United States")
    assert best_place(options, "Brooklyn, NY") == "Brooklyn, New York, United States"
    assert best_place(("Pune, Maharashtra, India",), "Pune, India") == "Pune, Maharashtra, India"
    assert best_place(("Paris, France",), "Pune, India") is None


@pytest.mark.parametrize(
    "hostile",
    ["", "   ", "\x00", "=cmd|' /C calc'!A0", "<script>alert(1)</script>", "x" * 10_000, "\u202eevil"],
)
def test_hostile_labels_and_answers_never_crash(hostile: str) -> None:
    f = FormField("k", hostile, "select", True, (hostile, "Yes", "No"))
    suggest(f, ctx(answers={hostile: hostile}))
    assert to_date(hostile) is None or to_date(hostile)


def test_country_of_residence_questions_use_the_home_country() -> None:
    abroad = replace(PERSON, needs_sponsorship=True, work_authorized=False)  # facts for a US job
    eligible = yes_no("Are you currently eligible to work in your country of residence?")
    sponsor = yes_no(
        "Do you now or in the future require visa sponsorship to continue working in your country of residence?"
    )
    assert suggest(eligible, ctx(abroad)).value == "Yes"  # type: ignore[union-attr]
    assert suggest(sponsor, ctx(abroad)).value == "No"  # type: ignore[union-attr]
    job_country = yes_no("Are you legally authorized to work in the country you are applying to?")
    assert suggest(job_country, ctx(abroad)).value == "No"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("label", "options"),
    [
        (
            "Which of the following best describes you?",
            ("I am an AI or automated program", "I am a human being"),
        ),
        ("Are you a robot?", ("Yes", "No")),
        ("Is this application being submitted by an automated tool or bot?", ("Yes", "No")),
    ],
)
def test_bot_checks_are_only_ever_answered_by_the_user(label: str, options: tuple[str, ...]) -> None:
    f = field(label, "select", options)
    assert suggest(f, ctx()) is None
    assert f in unanswered([f], ctx())
    own = replace(PERSON, saved_answers={label: options[-1]})
    assert suggest(f, ctx(own)).value == options[-1]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("label", "specific"),
    [
        ("Why do you want to work here?", True),
        ("Why are you interested in this role?", True),
        ("What excites you about our company?", True),
        ("Why Acme? Why do you want to join us?", True),
        ("Are you legally authorized to work in India?", False),
        ("Notice period", False),
        ("Why are you leaving your current job?", False),
    ],
)
def test_questions_about_one_employer_are_told_apart(label: str, specific: bool) -> None:
    from sarthi.jobs.forms import company_specific  # noqa: PLC0415

    assert company_specific(label) is specific


def test_an_answer_about_one_employer_never_reaches_another_form() -> None:
    from sarthi.jobs.forms import for_job, split_bank  # noqa: PLC0415

    # Seen live: "I'll remember it for other applications" for "Why do you want to work here?" at Acme.
    bank = {
        "Notice period": "30 days",
        for_job("Why do you want to work here?", "acme"): "Your FastAPI stack.",
    }
    why = FormField("why", "Why do you want to work here?", "textarea", True)
    general, own = split_bank(bank, "acme")
    assert suggest(why, ctx(replace(PERSON, saved_answers=general), answers=own)) is not None
    general, own = split_bank(bank, "beta")
    assert own == {}
    assert suggest(why, ctx(replace(PERSON, saved_answers=general), answers=own)) is None
    notice = FormField("np", "Notice period", "text", True)
    assert suggest(notice, ctx(replace(PERSON, saved_answers=general), answers=own)) is not None


# ---- users outside India (fictional) -----------------------------------------------------------------------

LONDON = Applicant(
    first_name="Maya", last_name="Fenwick", phone="+44 20 7946 0000", location="London, UK",
    country="United Kingdom", home_country="GB", authorized_countries=frozenset({"GB", "IE"}),
    work_authorized=True, needs_sponsorship=False,  # the job's own country is the UK
)  # fmt: skip


def test_a_question_naming_another_country_is_answered_for_that_country() -> None:
    yes_no = ("Yes", "No")
    us = field("Are you legally authorized to work in the United States?", "select", yes_no)
    assert suggest(us, ctx(LONDON)).value == "No"  # type: ignore[union-attr]
    visa = field("Will you require visa sponsorship to work in the US?", "select", yes_no)
    assert suggest(visa, ctx(LONDON)).value == "Yes"  # type: ignore[union-attr]
    ireland = field("Are you eligible to work in Ireland?", "select", yes_no)
    assert suggest(ireland, ctx(LONDON)).value == "Yes"  # type: ignore[union-attr]
    unnamed = field("Are you legally authorized to work in the country of this job?", "select", yes_no)
    assert suggest(unnamed, ctx(LONDON)).value == "Yes"  # type: ignore[union-attr]  (the job's country)


def test_country_and_dial_code_options_are_found_however_written() -> None:
    country = field("Country", "select", ("India", "UK", "United States"))
    assert suggest(country, ctx(LONDON)).value == "UK"  # type: ignore[union-attr]
    dial = field("Phone country code", "select", ("India (+91)", "United Kingdom (+44)", "Jersey (+44)"))
    assert suggest(dial, ctx(LONDON)).value == "United Kingdom (+44)"  # type: ignore[union-attr]
    uae = replace(LONDON, home_country="AE", country="United Arab Emirates", phone="")
    assert suggest(field("Country", "select", ("Saudi Arabia", "UAE")), ctx(uae)).value == "UAE"  # type: ignore[union-attr]


def test_a_typed_start_date_is_read_as_the_user_writes_dates() -> None:
    start = FormField("start", "Earliest start date", "date", True)
    london = ctx(LONDON, answers={"Earliest start date": "04/11/2026"})
    assert suggest(start, london).value == "2026-11-04"  # type: ignore[union-attr]
    new_york = replace(LONDON, home_country="US")
    assert suggest(start, ctx(new_york, answers={"Earliest start date": "04/11/2026"})).value == "2026-04-11"  # type: ignore[union-attr]
