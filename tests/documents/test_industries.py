"""Tailoring for jobs outside software: nursing, teaching, accounting, sales, engineering, design.

Every person, employer and number is fictional.
"""

from typing import Any

import pytest

from sarthi.ai.documents import CoverAnswer, ResumeChange, change_sections, sections_text
from sarthi.documents import render
from sarthi.documents.build import cover_letter, greeting_for, letter_company, named_company, plain_letter
from sarthi.documents.diffs import apply_changes, count_problems, line_problems, tense_problems
from sarthi.documents.handler import file_stem
from sarthi.documents.truth import echo_problems, title_problems
from sarthi.jobs.skills import skills_in
from sarthi.profile.master import (
    Contact,
    Extra,
    Job,
    MasterResume,
    School,
    SkillGroup,
    contact_from,
    name_from,
)


def ats_from_text(monkeypatch: pytest.MonkeyPatch, text: str, master: MasterResume) -> list[str]:
    """``render.ats_problems`` on a PDF whose text layer reads ``text``."""

    def read(_pdf: Any) -> tuple[str, int]:
        return text, 1

    monkeypatch.setattr(render, "pdf_text", read)
    return render.ats_problems(b"%PDF", master)


NIV = "Manage patients on non-invasive ventilation (CPAP and BiPAP), escalating deterioration using NEWS2."
DISCHARGE = "Run discharge planning with physiotherapists, occupational therapists and social workers."
ACCOUNTS = "Manage 85 hotel and restaurant accounts in Dubai with annual sales of AED 14 million."
SUMMARY = "Registered adult nurse with six years on acute medical and respiratory wards."
JOB = "Band 6 nurse for our respiratory unit. NIV, NEWS2 and current NMC registration essential."


def nurse() -> MasterResume:
    return MasterResume(
        name="Siobhán O'Connell-Barrett",
        headline="Registered Adult Nurse",
        contact=Contact(email="s.oconnell@example.com", phone="+44 7700 900123", location="Leeds, UK"),
        summary=SUMMARY,
        experience=(
            Job(
                company="Aire Valley Teaching Hospitals NHS Trust",
                title="Senior Staff Nurse",
                start="04/2022",
                end="Present",
                bullets=(NIV, DISCHARGE, "Led the ward's falls project; falls fell by 31% over 12 months."),
            ),
        ),
        education=(School(school="University of Leeds", degree="BSc (Hons) Adult Nursing"),),
        skills=(SkillGroup(label="Clinical", items=("NEWS2", "IV cannulation")),),
        extras=(Extra(heading="Registration", items=("NMC Registered Nurse (Adult)",)),),
    )


def change(path: str, original: str, value: str) -> ResumeChange:
    return ResumeChange(path=path, original=original, value=value)


# ---- truth gates with a field's own words ---------------------------------------------------------------


def test_an_acronym_of_words_the_line_writes_out_is_not_a_new_fact() -> None:
    new = "Manage patients on NIV (CPAP and BiPAP), escalating deterioration using NEWS2 escalation."
    assert not [p for p in line_problems(new, NIV) if "NIV" in p]


def test_a_real_new_fact_is_still_refused() -> None:
    job = f"{JOB} Experience of tracheostomy weaning and high-flow oxygen."
    new = "Manage patients on NIV and tracheostomy weaning, escalating deterioration using NEWS2."
    assert any("tracheostomy" in p for p in line_problems(new, NIV, job_text=job))
    new = "Manage patients on non-invasive ventilation, escalating deterioration using NEWS2 and ICNARC."
    assert any("ICNARC" in p for p in line_problems(new, NIV))


def test_a_letters_initials_must_be_spelled_out_in_order() -> None:
    assert any('"VIN"' in p for p in line_problems("Manage VIN patients, escalating using NEWS2.", NIV))


def test_the_fields_word_for_the_same_work_passes() -> None:
    new = "Run multidisciplinary discharge planning with physiotherapists, occupational therapists and social workers."
    assert line_problems(new, DISCHARGE) == []


def test_a_current_duty_keeps_its_present_tense() -> None:
    assert tense_problems("Ran discharge planning with the ward team.", DISCHARGE)
    assert not tense_problems("Run multidisciplinary discharge planning.", DISCHARGE)
    assert not tense_problems("Built a test bench.", "Built fixtures for gearbox tests.")


def test_the_summary_may_name_a_registration_written_elsewhere_on_the_resume() -> None:
    master = nurse()
    new = "NMC registered adult nurse with six years on respiratory and acute medical wards."
    applied = apply_changes(master, [change("summary", SUMMARY, new)], job_text=JOB)
    assert applied.resume.summary == new, applied.rejected


@pytest.mark.parametrize(
    "new",
    [
        "NMC registered adult nurse with six years on respiratory wards and ICU, holding an ALS certificate.",
        "NMC registered adult nurse with six years on respiratory wards, cutting falls by 31% with NEWS2.",
    ],
)
def test_the_summary_may_not_invent_or_borrow_bullet_details(new: str) -> None:
    applied = apply_changes(nurse(), [change("summary", SUMMARY, new)], job_text=JOB)
    assert applied.resume.summary == SUMMARY, new


def test_a_bullet_still_may_not_borrow_another_lines_facts() -> None:
    master = nurse()
    new = (
        "Run NEWS2-led discharge planning with physiotherapists, occupational therapists and social workers."
    )
    applied = apply_changes(master, [change("experience[0].bullets[1]", DISCHARGE, new)], job_text=JOB)
    assert applied.resume.experience[0].bullets[1] == DISCHARGE


def test_a_number_keeps_counting_what_it_counted() -> None:
    old = "Won 23 new accounts in 2023, including the central kitchens of two 5-star hotel groups."
    assert count_problems(
        "Won 23 new hotel accounts in 2023, including two 5-star hotel groups' kitchens.", old
    )
    assert not count_problems(
        "Won 23 new accounts in 2023, among them two 5-star hotel groups' kitchens.", old
    )
    summary = (
        "Chartered Accountant with 7 years in statutory audit and indirect tax for manufacturing clients."
    )
    assert not count_problems(
        "Chartered Accountant with 7 years in indirect tax and statutory audit.", summary
    )


def test_a_keyword_the_line_had_is_not_dropped() -> None:
    old = "Ran A/B tests on landing pages and offers, raising site conversion rate from 2.1% to 2.9%."
    new = "Ran landing page tests on offers, raising site conversion rate from 2.1% to 2.9%."
    assert any('drops "A/B"' in p for p in line_problems(new, old))


def test_reordering_the_lines_own_course_names_is_not_copying_the_posting() -> None:
    old = "Teach General Mathematics and Mathematical Methods to Years 11 and 12."
    new = "Teach Mathematical Methods and General Mathematics to Years 11 and 12."
    job = "Plan and teach Mathematical Methods and General Mathematics under the QCE system."
    assert echo_problems(new, job)
    assert not echo_problems(new, job, own=old)


@pytest.mark.parametrize(
    ("text", "absent"),
    [
        ("Teach a 0.6 load and use the rest of their time to lead numeracy work.", "REST"),
        ("Knowledge of iOS and Android design guidelines.", "Swift"),
        ("Candidates who excel at relationships and give a swift reply.", "Excel"),
    ],
)
def test_everyday_words_are_not_skills(text: str, absent: str) -> None:
    assert absent not in skills_in(text)


def test_skills_written_as_skills_are_still_found() -> None:
    assert {"REST", "Swift", "Excel"} <= set(skills_in("RESTful APIs in Swift; reports in MS Excel."))


def test_contact_keeps_us_phone_brackets_and_portfolio_domains() -> None:
    found = contact_from("Jordan Ellis\nAustin, TX | (512) 555-0147 | fennadevries.design\n")
    assert found.phone == "(512) 555-0147"
    assert "fennadevries.design" in found.links


def test_a_name_in_two_scripts_is_read_back_by_its_latin_part(monkeypatch: pytest.MonkeyPatch) -> None:
    master = MasterResume(name="Omar Al-Haddad عمر الحداد", contact=Contact(email="omar@example.com"))
    pdf_words = "OMAR AL-HADDAD ادﻟﺤﺪا  ﻋﻤﺮ\nomar@example.com"
    assert not [p for p in ats_from_text(monkeypatch, pdf_words, master) if "name" in p]
    german = MasterResume(name="Jörg Müller-Weißenfels", contact=Contact(email="j@example.com"))
    capitals = "JÖRG MÜLLER-WEISSENFELS\nj@example.com"
    assert not [p for p in ats_from_text(monkeypatch, capitals, german) if "name" in p]
    assert [p for p in ats_from_text(monkeypatch, "J. MULLER\nj@example.com", german) if "name" in p]


def test_plain_letter_grammar() -> None:
    master = nurse().model_copy(
        update={
            "experience": (nurse().experience[0].model_copy(update={"title": "Audit Manager"}),),
            "skills": (SkillGroup(items=("Statutory audit", "Ind AS")),),
        }
    )
    text = " ".join(plain_letter(master, "Assistant Manager", "Acme", "Statutory audit under Ind AS."))
    assert "an Audit Manager" in text
    assert "statutory audit" in text
    assert "Ind AS" in text


@pytest.mark.parametrize(
    ("summary", "known"),
    [
        ("Numeracy Leader with 8 years in Queensland schools.", "Secondary teacher, Mathematics"),
        ("Discharge Coordinator with six years on acute wards.", "Registered adult nurse"),
        ("Key Account Manager selling to hotels in the UAE.", "Senior Sales Executive"),
        ("Finance Controller for manufacturing clients.", "Chartered Accountant, Audit Manager"),
    ],
)
def test_the_jobs_title_is_never_given_to_the_candidate(summary: str, known: str) -> None:
    assert title_problems(summary, known)


def test_extras_reach_the_prompts() -> None:
    assert "NMC Registered Nurse (Adult)" in change_sections(nurse())
    assert "NMC Registered Nurse (Adult)" in sections_text(nurse())


# ---- names, companies and file names --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "greeting"),
    [
        ("Emirates Fine Foods Supply L.L.C.", "Emirates Fine Foods Supply"),
        ("Oasis Hospitality Solutions FZ-LLC", "Oasis Hospitality Solutions"),
        ("Kanaal Payments B.V.", "Kanaal Payments"),
        ("Hessler Getriebe AG", "Hessler Getriebe"),
        ("Rheintal Antriebstechnik GmbH & Co. KG", "Rheintal Antriebstechnik"),
        ("Southern Cross Learning Pty Ltd", "Southern Cross Learning"),
        ("Thames Valley Care PLC", "Thames Valley Care"),
        ("SRINIVASA AGRO FOODS PRIVATE LIMITED", "Srinivasa Agro Foods"),
        ("Wise", "Wise"),
        ("Top Hat Inc.", "Top Hat"),
    ],
)
def test_greeting_company_drops_legal_forms(given: str, greeting: str) -> None:
    assert letter_company(given) == greeting


@pytest.mark.parametrize(
    "placeholder",
    [
        "",
        "Confidential",
        "Company name confidential",
        "Undisclosed",
        "A leading audit and assurance firm",
        "N/A",
    ],
)
def test_no_company_named_gives_a_plain_greeting(placeholder: str) -> None:
    assert not named_company(placeholder)
    assert greeting_for(placeholder) == "Dear Hiring Manager,"
    letter = " ".join(plain_letter(nurse(), "Staff Nurse", placeholder, JOB))
    assert " at ." not in letter
    assert "Confidential" not in letter
    assert "role." in letter


def test_a_letter_naming_the_placeholder_as_employer_is_sent_back() -> None:
    body = ["I would like to join Confidential as a nurse. " * 8, "I manage patients on ventilation. " * 10]
    letter = cover_letter(
        nurse(), CoverAnswer(paragraphs=body), "", role="Staff Nurse", company="Confidential", job_text=JOB
    )
    assert letter.greeting == "Dear Hiring Manager,"
    assert any("names no company" in p for p in letter.problems)


def test_a_short_letter_of_pasted_bullets_is_sent_back() -> None:
    body = [f"In my current post I {NIV[0].lower()}{NIV[1:]}", "I would like to talk."]
    letter = cover_letter(
        nurse(), CoverAnswer(paragraphs=body), "", role="Nurse", company="Pennine", job_text=JOB
    )
    assert any("words" in p for p in letter.problems)
    assert any("pastes the resume line" in p for p in letter.problems)


@pytest.mark.parametrize(
    ("name", "company", "stem"),
    [
        ("Jörg Müller-Weißenfels", "Voltara Mobility GmbH", "Jorg_Muller_Weissenfels_Voltara_Mobility"),
        ("Nguyễn Thị Minh Anh", "Lakeview Mutual", "Nguyen_Thi_Minh_Anh_Lakeview_Mutual"),
        ("Omar Al-Haddad عمر الحداد", "Gulf Horizon L.L.C.", "Omar_Al_Haddad_Gulf_Horizon"),
        ("Siobhán O'Connell", "Confidential", "Siobhan_O_Connell"),
    ],
)
def test_file_names_keep_accented_names_readable(name: str, company: str, stem: str) -> None:
    assert file_stem(name, company) == stem


@pytest.mark.parametrize(
    "name", ["Siobhán O'Connell-Barrett", "Nguyễn Thị Minh Anh", "Jörg Müller-Weißenfels", "عمر الحداد"]
)
def test_names_in_any_script_are_found(name: str) -> None:
    assert name_from(f"{name}\nRegistered Nurse\nLeeds, UK | +44 7700 900123") == name
