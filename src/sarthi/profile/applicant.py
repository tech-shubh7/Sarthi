"""The facts an application form is filled with, for one job.

They come from the profile, the master resume and the answers the user gave on earlier forms.

Work authorisation and sponsorship depend on the job's country: a person in India needs no visa for an Indian
job and does for a US one. When the job's country is unknown, the answer is unknown and the question goes to
the user; nothing is guessed.
"""

from __future__ import annotations

import re
from pathlib import Path

from sarthi.db.answers import AnswerStore
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.jobs.countries import name as country_name
from sarthi.jobs.forms import Applicant, FormContext, split_bank
from sarthi.jobs.models import JobPosting, RemoteType
from sarthi.profile.master import MasterResume, load_master, master_path
from sarthi.profile.models import Profile

_PRESENT = re.compile(r"(?i)present|current|now|ongoing|today")
_YEAR = re.compile(r"(?:19|20)\d{2}")


def _split_name(name: str) -> tuple[str, str]:
    parts = name.split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _link(links: tuple[str, ...], host: str) -> str:
    found = next((link for link in links if host in link.lower()), "")
    return found if not found or found.startswith("http") else f"https://{found}"


def _year(value: str) -> str:
    years = _YEAR.findall(value)
    return years[-1] if years else ""


def job_countries(job: JobPosting) -> frozenset[str]:
    """The countries the job may be done from: its remote countries, else where it is located."""
    if job.remote_type is RemoteType.REMOTE:
        return job.remote_countries
    return job.country_codes


def visa_facts(profile: Profile, job: JobPosting) -> tuple[bool | None, bool | None]:
    """(work authorised, needs sponsorship) for this job; (None, None) when its country is unknown."""
    prefs = profile.preferences
    if job.is_remote_anywhere:
        return True, False
    countries = job_countries(job)
    if not countries:
        return None, None
    authorised = bool(countries & prefs.visa_free_countries)
    return authorised, not authorised


def relocation(profile: Profile, job: JobPosting) -> bool | None:
    """Willing to relocate for this job: yes when its country is one the user works in, no when it is not."""
    if job.remote_type is RemoteType.REMOTE or not job.country_codes:
        return None
    return bool(job.country_codes & profile.preferences.work_countries)


def salary_text(profile: Profile) -> str:
    """The user's salary floor as a form answer ("1200000 INR per year"), or ""."""
    floor = profile.preferences.min_salary
    if floor is None or floor.minimum is None:
        return ""
    return f"{floor.minimum:.0f} {floor.currency} per {floor.period.value}"


def applicant(
    profile: Profile, master: MasterResume | None, job: JobPosting, saved: dict[str, str]
) -> Applicant:
    """Everything this job's form may be filled with."""
    master = master or MasterResume()
    first, last = _split_name(master.name)
    links = master.contact.links
    latest = master.experience[0] if master.experience else None
    current = latest is not None and bool(_PRESENT.search(latest.end))
    school = master.education[0] if master.education else None
    authorised, sponsorship = visa_facts(profile, job)
    home = country_name(profile.preferences.home_country)
    website = next((link for link in links if "linkedin" not in link and "github" not in link), "")
    return Applicant(
        first_name=first,
        last_name=last,
        email=master.contact.email,
        phone=master.contact.phone,
        linkedin=_link(links, "linkedin"),
        github=_link(links, "github"),
        website=website,
        location=master.contact.location or home,
        country=home,
        needs_sponsorship=sponsorship,
        work_authorized=authorised,
        willing_to_relocate=relocation(profile, job),
        salary_expectation=salary_text(profile),
        current_company=latest.company if current and latest else "",
        current_title=latest.title if current and latest else "",
        school=school.school if school else "",
        degree=school.degree if school else "",
        start_year=_year(school.start) if school else "",
        end_year=_year(school.end) if school else "",
        saved_answers=dict(saved),
    )


LETTER_FILE = "cover_letter.txt"


def form_context(  # noqa: PLR0913  (everything a form is filled from)
    job_id: str,
    profile: Profile,
    *,
    jobs: JobStore,
    documents: DocumentStore,
    answers: AnswerStore,
    data_dir: Path,
) -> FormContext | None:
    """What this job's form may be filled with (facts, files, letter, answers); None when the job is gone."""
    job = jobs.get(job_id)
    if job is None:
        return None
    record = documents.get(job_id)
    resume = cover = letter = ""
    if record is not None:
        resume, cover = str(data_dir / record.resume_file), str(data_dir / record.letter_file)
        letter_path = data_dir / Path(record.resume_file).parent / LETTER_FILE
        letter = letter_path.read_text(encoding="utf-8") if letter_path.is_file() else ""
    master = load_master(master_path(data_dir))
    general, own = split_bank(answers.saved(), job_id)
    return FormContext(
        applicant=applicant(profile, master, job, general),
        answers=own,
        resume_file=resume,
        cover_file=cover,
        cover_letter=letter,
        job_location=job.location_text,
        job_remote=job.is_remote_anywhere,
    )
