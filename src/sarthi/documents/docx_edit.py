"""Tailored lines written into the user's own Word file, keeping its styles; a PDF when LibreOffice exists.

python-docx (MIT) opens the file; a changed line's words go into the paragraph's first run, which keeps its
font, size and colour, and the paragraph keeps its style, bullet and spacing. Paragraphs holding a hyperlink
are never rewritten (a link would be lost); headers, footers and every other paragraph stay as they are.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docx

from sarthi.documents.build import Edit

CONVERT_TIMEOUT_S = 120


@dataclass
class DocxResult:
    """The edited file and which edits were made."""

    docx: bytes
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _paragraphs(container: Any) -> Iterator[Any]:
    yield from container.paragraphs
    for table in getattr(container, "tables", []):
        for row in table.rows:
            for cell in row.cells:
                yield from _paragraphs(cell)


def _set_text(paragraph: Any, new: str) -> bool:
    runs = [run for run in paragraph.runs if run.text]
    if not runs or paragraph._p.xpath(".//w:hyperlink"):
        return False
    runs[0].text = new
    for run in runs[1:]:
        run.text = ""
    return True


def _replace(document: Any, edit: Edit) -> bool:
    target = _key(edit.old)
    paragraphs = list(_paragraphs(document))
    for paragraph in paragraphs:
        if _key(paragraph.text) == target:
            return _set_text(paragraph, edit.new)
    for paragraph in paragraphs:  # a part of one run, e.g. a skills list after its bold label
        for run in paragraph.runs:
            if edit.old in run.text:
                run.text = run.text.replace(edit.old, edit.new, 1)
                return True
    return False


def edit_docx(data: bytes, edits: list[Edit]) -> DocxResult:
    """The Word file with each edit's line re-worded in place; lines not found are skipped."""
    document = docx.Document(io.BytesIO(data))
    result = DocxResult(b"")
    for edit in edits:
        (result.applied if _replace(document, edit) else result.skipped).append(edit.where)
    out = io.BytesIO()
    document.save(out)
    result.docx = out.getvalue()
    return result


def converter() -> str | None:
    """LibreOffice, when installed."""
    return shutil.which("soffice") or shutil.which("libreoffice")


def docx_to_pdf(data: bytes) -> bytes | None:
    """The Word file as PDF via LibreOffice; None when it is not installed or the conversion fails."""
    program = converter()
    if program is None:
        return None
    with tempfile.TemporaryDirectory(prefix="sarthi-docx-") as tmp:
        folder = Path(tmp)
        (folder / "resume.docx").write_bytes(data)
        command = [
            program, f"-env:UserInstallation={folder.as_uri()}/profile", "--headless", "--convert-to", "pdf",
            "--outdir", tmp, str(folder / "resume.docx"),
        ]  # fmt: skip
        try:
            subprocess.run(  # noqa: S603  (fixed arguments, no shell)
                command, capture_output=True, timeout=CONVERT_TIMEOUT_S, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        pdf = folder / "resume.pdf"
        return pdf.read_bytes() if pdf.is_file() else None
