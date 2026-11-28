"""What the browser extension fills into a form the user applies on themselves.

Indeed, Naukri and Workday block or wall the application browser, so those jobs go to "Apply yourself". In the
user's own browser, the Sarthi extension reads the form the user has open and asks the user's own Sarthi what
to put in each field. The answers come from the same rules as Sarthi's own applications (``suggest``): the
profile, the saved answers, this job's tailored resume and letter. Nothing is guessed, and the user presses
Submit themselves: the extension never does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sarthi.db.answers import AnswerStore
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.jobs.forms import FormField, company_specific, for_job, suggest
from sarthi.profile.applicant import form_context
from sarthi.profile.models import Profile

MAX_FIELDS = 200  # a real application form has far fewer: anything more is not a form
FILE_KINDS = ("resume", "cover")
SECRET = re.compile(
    r"pass(?:word|code|phrase)|\botp\b|one[- ]time|verification code|captcha"
    r"|security (?:code|answer|question)|\bpin\b|social security|\bssn\b|aadhaa?r|\bpan\b"
    r"|passport (?:no|number)|national id|tax id|bank|card number|\bcvv\b|iban|routing",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Answer:
    """One field's value and where it came from ("profile", "saved", "job", "default", "file")."""

    value: str
    source: str


@dataclass
class AssistFill:
    """The answers for one form, the required questions left to the user and the documents to attach."""

    answers: dict[str, Answer] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)  # keys of required fields only the user can answer
    resume: str = ""  # the tailored resume's file name ("" when it is not made yet)
    cover: str = ""


def assist_fill(  # noqa: PLR0913  (the form and everything it is filled from)
    job_id: str,
    profile: Profile,
    fields: list[FormField],
    *,
    jobs: JobStore,
    documents: DocumentStore,
    answers: AnswerStore,
    data_dir: Path,
) -> AssistFill | None:
    """The values for ``fields`` of this job's form; None when the job is not stored."""
    ctx = form_context(job_id, profile, jobs=jobs, documents=documents, answers=answers, data_dir=data_dir)
    if ctx is None:
        return None
    result = AssistFill(
        resume=Path(ctx.resume_file).name if ctx.resume_file else "",
        cover=Path(ctx.cover_file).name if ctx.cover_file else "",
    )
    for form_field in fields[:MAX_FIELDS]:
        hit = suggest(form_field, ctx)
        if hit is None:
            if form_field.required and form_field.type != "file":
                result.missing.append(form_field.key)
            continue
        if hit.source == "file":  # never a path on this computer: the extension asks for the file by kind
            kind = "cover" if "cover" in form_field.label.lower() else "resume"
            result.answers[form_field.key] = Answer(kind, "file")
        else:
            result.answers[form_field.key] = Answer(hit.value, hit.source)
    return result


def remember(answers: AnswerStore, job_id: str, given: list[tuple[str, str]]) -> int:
    """Keep what the user typed into questions Sarthi could not answer, for the next forms.

    A question about this one employer ("Why do you want to work here?") is kept for this job only, as in
    Telegram. Returns how many were kept.
    """
    kept = 0
    for raw_label, raw_value in given[:MAX_FIELDS]:
        label, value = raw_label.strip(), raw_value.strip()
        if not label or not value or SECRET.search(label):
            continue  # a password, a one-time code or an ID number is never kept
        answers.save(for_job(label, job_id) if company_specific(label) else label, value)
        kept += 1
    return kept
