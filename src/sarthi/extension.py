"""The browser extension's side in the running app: pairing, the job list, the answers and the package.

``sarthi.web.extension_api`` has the checks every call passes; ``sarthi.profile.assist`` has the answers.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import secrets
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from sarthi import web
from sarthi.apply import adapters
from sarthi.clock import Clock, to_iso
from sarthi.db.answers import AnswerStore
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.documents import DocumentStore
from sarthi.db.jobs import JobStore
from sarthi.jobs.forms import FormField
from sarthi.profile.assist import AssistFill, assist_fill, remember
from sarthi.profile.models import Profile
from sarthi.queue import names
from sarthi.queue.tasks import Priority, TaskQueue
from sarthi.web.actions import ExtensionJob, ExtensionJobs

CODE_KEY = "extension.code"  # "<sha256 of the code>|<expires at>"
KEY_KEY = "extension.key"  # sha256 of the paired extension's key
CODE_LIFE = timedelta(minutes=10)
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I/L to misread
CODE_LENGTH = 8
SOURCE = Path(web.__file__).parent / "extension"
SCAN_JS = Path(adapters.__file__).parent / "scan.js"
ICONS = {
    "icon-32.png": Path(web.__file__).parent / "static" / "brand" / "sarthi-favicon-32.png",
    "icon-128.png": Path(web.__file__).parent / "static" / "brand" / "apple-touch-icon.png",
}
NOTES = {
    ApplicationStatus.NEEDS_HUMAN: "Apply yourself",
    ApplicationStatus.FILLED: "Filled by Sarthi, waiting for your Submit",
}


class _State(Protocol):
    def get_state(self, key: str) -> str | None: ...
    def set_state(self, key: str, value: str | None) -> None: ...


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ExtensionService:
    """What the extension may read and record, through the app's own stores."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        *,
        state: _State,
        jobs: JobStore,
        documents: DocumentStore,
        answers: AnswerStore,
        applications: ApplicationStore,
        queue: TaskQueue,
        load_profile: Callable[[], Profile | None],
        mark_applied: Callable[[str], bool],
        data_dir: Path,
        clock: Clock,
    ) -> None:
        """Use the app's stores; ``mark_applied`` is the dashboard's "I applied"."""
        self.state, self.jobs, self.documents = state, jobs, documents
        self.answers, self.applications, self.queue = answers, applications, queue
        self.load_profile, self.mark_applied = load_profile, mark_applied
        self.data_dir, self.clock = data_dir, clock

    # ---- pairing ----

    def code(self) -> str:
        """A new one-time code (the older one stops working)."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        self.state.set_state(CODE_KEY, f"{_hash(code)}|{to_iso(self.clock() + CODE_LIFE)}")
        return code

    def pair(self, code: str) -> str | None:
        """A new key for the right, unexpired code; the code is used up and any older key ends."""
        stored = self.state.get_state(CODE_KEY) or ""
        digest, _, until = stored.partition("|")
        try:
            expired = datetime.fromisoformat(until) <= self.clock()
        except ValueError:
            return None
        if not digest or expired or not hmac.compare_digest(_hash(code.strip().upper()), digest):
            return None
        self.state.set_state(CODE_KEY, None)
        key = secrets.token_urlsafe(32)
        self.state.set_state(KEY_KEY, _hash(key))
        return key

    def allowed(self, key: str) -> bool:
        """Whether ``key`` is the paired extension's."""
        stored = self.state.get_state(KEY_KEY) or ""
        return bool(stored) and bool(key) and hmac.compare_digest(_hash(key), stored)

    # ---- the jobs and their forms ----

    def jobs_for(self, url: str) -> ExtensionJobs:
        """The job at ``url`` first (when known), then the ones waiting for the user."""
        matched = self.jobs.find_by_url(url) if url else None
        listed: list[ExtensionJob] = []
        seen: set[str] = set()
        if matched and (job := self.jobs.get(matched)) is not None:
            record = self.applications.get(matched)
            note = NOTES.get(record.status, "") if record else ""
            listed.append(ExtensionJob(matched, job.title, job.company, note or "This page"))
            seen.add(matched)
        for record in self.applications.waiting_for_user():
            if record.job_id in seen or (job := self.jobs.get(record.job_id)) is None:
                continue
            listed.append(ExtensionJob(record.job_id, job.title, job.company, NOTES.get(record.status, "")))
            seen.add(record.job_id)
        return ExtensionJobs(matched if matched in seen else None, listed)

    @staticmethod
    def scan_config(url: str) -> dict[str, str]:
        """The scanner's selectors for the hiring system at ``url`` (Greenhouse, Lever, Ashby or any form)."""
        return adapters.adapter_for(url).scan_config()

    def fill(self, job_id: str, fields: list[FormField]) -> AssistFill | None:
        """The answers for the form; the tailored documents are started when this job has none yet."""
        profile = self.load_profile()
        if profile is None or self.jobs.get(job_id) is None:
            return None
        result = assist_fill(
            job_id,
            profile,
            fields,
            jobs=self.jobs,
            documents=self.documents,
            answers=self.answers,
            data_dir=self.data_dir,
        )
        if result is not None and not result.resume:
            self.queue.enqueue(names.DOCUMENTS, job_id, {"apply": False}, priority=Priority.ALERT)
        return result

    def file(self, job_id: str, kind: str) -> Path | None:
        """This job's tailored resume or cover letter PDF (never a file outside the data folder)."""
        record = self.documents.get(job_id)
        if record is None:
            return None
        relative = record.resume_file if kind == "resume" else record.letter_file
        root = self.data_dir.resolve()
        path = (self.data_dir / relative).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != ".pdf" or not path.is_file():
            return None
        return path

    def applied(self, job_id: str, given: list[tuple[str, str]]) -> bool:
        """Recorded as applied by the user; their answers to new questions are kept for the next forms."""
        job = self.jobs.get(job_id)
        if job is None:
            return False
        remember(self.answers, job_id, given)
        if not self.mark_applied(job_id):  # without Telegram's flow: recorded straight away
            site = adapters.host_of(str(job.apply_url or job.url))
            self.applications.record(job_id, ApplicationStatus.APPLIED_BY_USER, site=site)
        return True

    # ---- the package ----

    @staticmethod
    def package() -> bytes:
        """The extension as a .zip: its own files, the form scanner Sarthi itself uses, and the icons."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(SOURCE.iterdir()):
                if path.is_file():
                    zipped.write(path, path.name)
            scanner = SCAN_JS.read_text(encoding="utf-8").strip().rstrip(";")
            zipped.writestr(
                "scan.js",
                f"// Sarthi's own form scanner (sarthi/apply/scan.js).\nself.__sarthiScan = {scanner};\n",
            )
            for name, icon in ICONS.items():
                zipped.write(icon, name)
        return buffer.getvalue()
