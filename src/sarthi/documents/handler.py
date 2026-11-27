"""The ``documents`` task: a tailored resume, a cover letter and an ATS keyword report for one job.

Made only for jobs the user tapped Apply (or "Tailored resume") on, not for every match, so AI calls stay few.
Steps: master resume (read once per uploaded resume, kept only where the resume text confirms it) → resume:
the person's own file unchanged, or (``tailor_resume``) re-made as a page with the AI's verified line changes
for this job → cover letter (AI words, code facts, one more try) → PDFs → read-back check → keyword report →
a ``documents_ready`` task for the chat.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sarthi.ai.client import AiClient, BudgetExhaustedError
from sarthi.ai.documents import ResumeChange, read_master, tailor_changes, unsupported_claims
from sarthi.ai.documents import cover_letter as ai_cover_letter
from sarthi.clock import Clock, utcnow
from sarthi.db.documents import DocumentRecord, DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.documents.build import (
    MIN_LETTER_WORDS,
    Built,
    Letter,
    cover_letter,
    edits,
    letter_company,
    plain_letter,
    tailored_resume,
    without_claims,
)
from sarthi.documents.diffs import Applied, Changed, apply_changes, term_hints
from sarthi.documents.html_pdf import HtmlPdfError
from sarthi.documents.html_resume import page_like, resume_pdf_html
from sarthi.documents.inplace import OwnDesign, contacts_kept, own_design
from sarthi.documents.keywords import keyword_report
from sarthi.documents.render import (
    RenderError,
    ats_problems,
    letter_pdf,
    paper_for,
    pdf_text,
    resume_pdf,
    write,
)
from sarthi.profile.master import (
    MasterResume,
    contact_from,
    grounded,
    load_master,
    master_path,
    merged_links,
    name_from,
    save_master,
)
from sarthi.profile.models import Profile
from sarthi.profile.original import load_original, text_hash
from sarthi.queue import names
from sarthi.queue.tasks import MAX_ATTEMPTS, DeferError, PermanentError, Priority, RetryableError, TaskQueue

log = logging.getLogger(__name__)

WAIT_FOR_PROFILE_S = 3600
AI_BUSY_WAIT_S = 90.0  # free AI tiers limit tokens per minute: better documents after a short wait
BUSY_BACKOFF = 4
LAST_TRY = MAX_ATTEMPTS  # on the queue's last attempt, the plain fallbacks are accepted
DOCUMENTS_DIR = "documents"
NO_RESUME = "Tailored documents need your resume: send /setup and upload it."
NO_AI = "Tailored documents need an AI model: set SARTHI_LLM_QUALITY_MODELS (see .env.example)."


@dataclass(frozen=True, slots=True)
class Tries:
    """Which attempt of the task this is; a busy AI is waited for longer each time.

    Free tiers reset per minute, then per day: 90 s, 6 min, 24 min, 1.6 h. The last attempt takes what it can.
    """

    attempt: int

    @property
    def last(self) -> bool:
        """True on the queue's last attempt: the plain fallbacks are accepted."""
        return self.attempt >= LAST_TRY

    @property
    def wait(self) -> float:
        """Seconds to wait before the next attempt when the AI is busy."""
        return AI_BUSY_WAIT_S * float(BUSY_BACKOFF) ** max(0, self.attempt - 1)


def resume_hash(text: str) -> str:
    """Identifies the uploaded resume a master resume was read from."""
    return text_hash(text)


@dataclass
class Tailoring:
    """The tailored resume, the lines changed and the AI changes thrown away (with why)."""

    resume: MasterResume
    changed: list[Changed] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    answered: bool = False  # False when no AI answered (the resume is the person's own words)


def _path(change: ResumeChange) -> str:
    return change.path.replace(" ", "")


# Letters with no accent to strip: written out as they are said ("Weißenfels" -> "Weissenfels").
_LATIN = str.maketrans(
    {"ß": "ss", "ẞ": "SS", "Æ": "AE", "æ": "ae", "Ø": "O", "ø": "o", "Đ": "D", "đ": "d", "Ł": "L", "ł": "l",
     "Œ": "OE", "œ": "oe", "Þ": "Th", "þ": "th", chr(0x131): "i"}  # the last is the dotless i
)  # fmt: skip


def _ascii(text: str) -> str:
    """``text`` in plain letters: "Jörg Weißenfels" -> "Jorg Weissenfels"; other scripts are left out."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_LATIN))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def file_stem(name: str, company: str) -> str:
    """A file name part like "Asha_Rao_Acme": safe on every file system, readable for recruiters."""
    parts = [
        re.sub(r"[^A-Za-z0-9]+", "_", _ascii(part)).strip("_") for part in (name, letter_company(company))
    ]
    return "_".join(p for p in parts if p)[:80] or "Sarthi"


class DocumentMaker:
    """Handles ``documents`` tasks."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        jobs: JobStore,
        documents: DocumentStore,
        queue: TaskQueue,
        load_profile: Callable[[], Profile | None],
        *,
        ai: AiClient,
        data_dir: Path,
        clock: Clock = utcnow,
        tailor_resume: bool = False,
    ) -> None:
        """Read jobs and the profile; write files under ``data_dir``; ask ``ai`` (the quality models).

        ``tailor_resume`` False (as ``SARTHI_TAILOR_RESUME`` by default): every job gets the person's own
        resume unchanged; only the letter is new. True: the resume is re-made as a page in its own layout with
        the AI's verified changes for the job.
        """
        self.tailor_resume = tailor_resume
        self.jobs = jobs
        self.documents = documents
        self.queue = queue
        self._load_profile = load_profile
        self.ai = ai
        self.data_dir = data_dir
        self._clock = clock

    async def master(self, profile: Profile, *, last: bool = False) -> MasterResume:
        """The master resume for the current upload, read by the AI once and then kept.

        When the AI cannot read it on the ``last`` try, the master read from an earlier upload is used as it
        is (not saved as this upload's); with none, the documents fail with a reason, never silently.
        """
        text = profile.resume_text
        if not text.strip():
            raise PermanentError(NO_RESUME)
        path = master_path(self.data_dir)
        saved = load_master(path)
        if saved is not None and saved.source_hash == resume_hash(text):
            found = contact_from(text)  # read again: newer versions find more (place, links behind PDF links)
            contact = saved.contact.model_copy(
                update={
                    "location": saved.contact.location or found.location,
                    "links": merged_links(saved.contact.links, found.links),  # never fewer than the resume's
                }
            )
            return saved if contact == saved.contact else saved.model_copy(update={"contact": contact})
        if not self.ai.available:
            raise PermanentError(NO_AI)
        draft = await read_master(self.ai, text)
        if draft is None and not last:
            raise RetryableError("the AI could not read the resume into sections right now")
        if draft is None and saved is not None:
            log.warning("the AI could not read the new resume: using the one read from the earlier upload")
            return saved
        if draft is None:
            raise PermanentError(
                "The AI could not read your resume into sections after several tries (busy or out of quota)."
            )
        master = grounded(draft, text).model_copy(
            update={"contact": contact_from(text), "source_hash": resume_hash(text)}
        )
        name = master.name or name_from(text)
        master = master.model_copy(update={"name": name.title() if name.isupper() else name})  # "ASHA RAO"
        save_master(path, master)
        return master

    async def handle(self, task: TaskRecord) -> None:
        """Make the documents for the job named by ``task.key``; always tell the chat how it went."""
        profile = self._load_profile()
        if profile is None:
            raise DeferError(WAIT_FOR_PROFILE_S)
        if task.payload.get("apply") and self._made_already(task.key, profile):
            self._ready(task, {"error": None, "reused": True})  # sent before: not made or sent again
            return
        try:
            report = await self.make(task.key, profile, tries=Tries(task.attempts))
        except BudgetExhaustedError as error:
            raise DeferError(error.retry_after) from error
        except PermanentError as error:
            self._ready(task, {"error": str(error)})
            return
        self._ready(task, {"error": None, **report})

    def _made_already(self, job_id: str, profile: Profile) -> bool:
        """True when this job's documents exist for the current profile (e.g. from "Tailored resume")."""
        record = self.documents.get(job_id)
        return (
            record is not None
            and record.profile_version == profile.version
            and all((self.data_dir / path).is_file() for path in (record.resume_file, record.letter_file))
        )

    def _ready(self, task: TaskRecord, result: dict[str, Any]) -> None:
        self.queue.enqueue(
            names.DOCUMENTS_READY, task.key, {**task.payload, **result}, priority=Priority.ALERT
        )

    def _busy(self, answer: object, tries: Tries) -> None:
        """No AI answer although a model is set up: wait and try again, unless this is the last try."""
        if answer is None and self.ai.available and not tries.last:
            raise RetryableError("the AI is busy (rate limit); trying again later", retry_after=tries.wait)

    async def make(self, job_id: str, profile: Profile, *, tries: Tries | None = None) -> dict[str, Any]:
        """Write the files and store the record. Returns the report.

        Unless this is the last try, a missing AI answer raises ``RetryableError``, so the task waits for the
        AI rather than settling for the untailored resume or the plain letter. ``tries`` None: a last try.
        """
        tries = tries or Tries(LAST_TRY)
        job = self.jobs.get(job_id)
        if job is None:
            raise PermanentError("That job is no longer stored.")
        job_text = f"{job.title}\n{job.company}\n\n{self.jobs.full_text(job_id) or job.description}"
        master = await self.master(profile, last=tries.last)
        skills = profile.skills

        paper = paper_for(profile.preferences.home_country)
        stem = file_stem(master.name, job.company)
        folder = self.data_dir / DOCUMENTS_DIR / job_id
        resume_file, letter_file = folder / f"{stem}_Resume.pdf", folder / f"{stem}_Cover_Letter.pdf"
        tailoring: Tailoring | None = None
        if (
            self.tailor_resume
        ):  # the person's resume re-made as a page, with the verified changes for this job
            tailoring = await self._tailored(master, profile, job_text, tries)
            built = Built(tailoring.resume, reverted=tailoring.rejected)
        else:  # the person's own resume as it is (their file, when kept); only the letter is written per job
            built = tailored_resume(master, None, profile.resume_text, skills, job_text=job_text)
        letter = await self._letter(
            master, profile, role=job.title, company=job.company, job_text=job_text, tries=tries
        )

        try:
            if tailoring is not None:
                design = OwnDesign(None, kind="html", applied=[c.where for c in tailoring.changed])
                resume_bytes = await self._page_resume(profile, built.resume, paper)
            else:
                design = self._own_design(profile, master, built.resume, folder / f"{stem}_Resume")
                resume_bytes = design.pdf if design.pdf is not None else resume_pdf(built.resume, paper)
            letter_bytes = letter_pdf(built.resume, letter, paper, today=self._today())
        except (RenderError, HtmlPdfError) as error:
            raise PermanentError(f"The PDF could not be made: {error}") from error
        dropped = contacts_kept(resume_bytes, master.contact)
        if dropped:  # never send a resume an employer cannot reach the person from
            raise PermanentError(
                f"The tailored resume would drop your {', '.join(dropped)}; it was not made."
            )

        write(resume_file, resume_bytes)
        write(letter_file, letter_bytes)
        write(folder / "cover_letter.txt", letter.text().encode("utf-8"))

        read_back, _ = pdf_text(resume_bytes)
        keywords = keyword_report(job_text, read_back, f"{profile.resume_text}\n{', '.join(skills)}", skills)
        report: dict[str, Any] = {
            "role": job.title,
            "company": job.company,
            "keywords": {
                "on_resume": keywords.on_resume,
                "have_not_shown": keywords.have_not_shown,
                "missing": keywords.missing,
                "score": keywords.score,
            },
            "ats": ats_problems(resume_bytes, built.resume, own_design=design.pdf is not None),
            "design": design.summary(),
            "tailored_by_ai": tailoring is not None and tailoring.answered,
            "letter_by_ai": letter.from_ai,
            "kept_original": built.reverted[:10],
            "changes": [
                {"where": c.where, "before": c.before, "after": c.after}
                for c in (tailoring.changed if tailoring is not None else [])
            ][:30],
        }
        self.documents.save(
            DocumentRecord(
                job_id=job_id,
                profile_version=profile.version,
                resume_file=str(resume_file.relative_to(self.data_dir)),
                letter_file=str(letter_file.relative_to(self.data_dir)),
                report=report,
            )
        )
        log.info(
            "documents for %s at %s: keywords %s",
            job.title,
            job.company,
            "none listed" if keywords.score is None else f"{keywords.score}%",
        )
        return report

    async def _tailored(
        self, master: MasterResume, profile: Profile, job_text: str, tries: Tries
    ) -> Tailoring:
        """The resume with the AI's verified changes for this job (one or two AI calls).

        The changes code throws away (a quote that does not match, a fact the line does not state, a cliché,
        a copied sentence, a wrong length) go back to the AI once; what passes from both answers is used.
        """
        skills = profile.skills

        def apply(changes: list[ResumeChange]) -> Applied:
            return apply_changes(
                master, changes, job_text=job_text, resume_text=profile.resume_text, own_skills=skills
            )

        hints = term_hints(master, job_text)
        answer = await tailor_changes(self.ai, master, job_text, own_skills=skills, hints=hints)
        self._busy(answer, tries)
        if answer is None:
            return Tailoring(master, answered=False)
        first = apply(answer.changes)
        if not first.retry:
            return Tailoring(first.resume, first.changed, first.rejected, answered=True)
        again = await tailor_changes(self.ai, master, job_text, first.retry[:12], skills, hints=hints)
        if again is None:
            return Tailoring(first.resume, first.changed, first.rejected, answered=True)
        thrown = {r.split(":", 1)[0] for r in first.rejected}
        kept = [c for c in answer.changes if _path(c) not in thrown]
        taken = {_path(c) for c in kept}
        retried = [c for c in again.changes if c.action == "replace" and _path(c) not in taken]
        second = apply([*kept, *retried])
        return Tailoring(second.resume, second.changed, second.rejected, answered=True)

    async def _page_resume(self, profile: Profile, resume: MasterResume, paper: str) -> bytes:
        """``resume`` printed in the one-column page layout.

        On the paper and in the page count of the person's own PDF, when it is kept.
        """
        original = load_original(self.data_dir, profile.resume_text)
        page, pages = page_like(
            original.data if original is not None and original.kind == "pdf" else None, paper
        )
        return await resume_pdf_html(resume, page, pages or 1)

    def _own_design(
        self, profile: Profile, master: MasterResume, tailored: MasterResume, base: Path
    ) -> OwnDesign:
        """The tailored resume in the user's own file, when the uploaded file was kept.

        The edited .tex or .docx is written next to the PDF (``base`` + its extension) for the user.
        """
        original = load_original(self.data_dir, profile.resume_text)
        if original is None:
            return OwnDesign(None)
        try:
            design = own_design(original, edits(master, tailored), master.contact)
        except (RuntimeError, ValueError, OSError, KeyError) as error:  # a damaged or unusual file
            log.warning("own design not used: %s", type(error).__name__)
            return OwnDesign(None, kind=original.kind, problems=["your file could not be edited"])
        if design.source is not None:
            write(base.with_suffix(design.suffix), design.source)
        return design

    async def _claims(self, master: MasterResume, text: str, tries: Tries) -> list[str]:
        """The AI reviewer's unsupported claims (waits for a busy AI unless this is the last try)."""
        found = await unsupported_claims(self.ai, master, text)
        self._busy(found, tries)
        return found or []

    async def _letter(  # noqa: PLR0913  (the letter's inputs)
        self,
        master: MasterResume,
        profile: Profile,
        *,
        role: str,
        company: str,
        job_text: str,
        tries: Tries,
    ) -> Letter:
        """A letter that passes the code checks and the claim reviewer.

        The first answer's problems (a number or skill the resume lacks, a cliché, a claim the reviewer cannot
        find) go back to the AI once; from that second answer the true paragraphs are kept. When that fails,
        a first answer that was true (code and reviewer found no untrue claim, only wording to improve) is
        sent rather than the plain letter, which is the last resort.
        """
        problems: list[str] = []
        letter = Letter("", [], "", from_ai=False)
        true_first: Letter | None = None
        for strict in (False, True):
            answer = await ai_cover_letter(
                self.ai, master, job_text, problems[:10] or None, company=letter_company(company)
            )
            self._busy(answer, tries)
            letter = cover_letter(
                master,
                answer,
                profile.resume_text,
                role=role,
                company=company,
                own_skills=profile.skills,
                strict=strict,
                job_text=job_text,
            )
            if not letter.from_ai:
                if answer is None or strict:
                    return letter
                problems = letter.problems  # untrue on the first try: ask again with what was wrong
                continue
            claims = await self._claims(master, "\n\n".join(letter.paragraphs), tries)
            if strict:
                if not claims:
                    return letter
                trimmed = without_claims(letter, claims)
                short = trimmed is not None and len(" ".join(trimmed.paragraphs).split()) < MIN_LETTER_WORDS
                if trimmed is not None and not (short and true_first is not None):
                    return trimmed
                break
            problems = letter.problems + [f"unsupported claim: {c}" for c in claims]
            if not problems:
                return letter
            cliched = any(p.startswith(("uses ", "contains ")) for p in letter.problems)
            if not claims and not cliched:  # true, its wording could be better: kept if the second try fails
                true_first = Letter(letter.greeting, letter.paragraphs, letter.sign_off, from_ai=True)
        if true_first is not None:
            return true_first
        return Letter(
            letter.greeting, plain_letter(master, role, company, job_text), letter.sign_off, from_ai=False
        )

    def _today(self) -> date:
        return self._clock().astimezone().date()
