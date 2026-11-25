"""The tailored resume in the user's own design: their file with only the changed lines' words replaced.

- LaTeX source: edited in place; compiled when a TeX engine (Tectonic, TeX Live) is installed.
- Word: edited in place; turned into PDF when LibreOffice is installed.
- PDF: each changed line rewritten where it stood, when the new words fit the same lines.

The result is used only when every contact detail of the original (email, phone, place, profile links) can
still be read or clicked; otherwise Sarthi's own layout is used and the reason is told to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sarthi.documents.build import Edit
from sarthi.documents.docx_edit import docx_to_pdf, edit_docx
from sarthi.documents.pdf_edit import edit_pdf
from sarthi.documents.render import pdf_text
from sarthi.documents.tex import compile_tex, edit_tex
from sarthi.documents.truth import contact_problems
from sarthi.profile.master import Contact
from sarthi.profile.original import Original
from sarthi.profile.resume import pdf_links


@dataclass
class OwnDesign:
    """The resume made from the user's own file."""

    pdf: bytes | None  # the tailored resume as PDF, None when this computer cannot make one
    source: bytes | None = None  # the edited .tex or .docx (None for a PDF upload)
    suffix: str = ".pdf"  # the source's extension
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # why the PDF cannot be used, in plain words
    kind: str = ""  # "pdf", "docx", "tex"; "" when no uploaded file is kept

    def summary(self) -> dict[str, object]:
        """For the documents report: was the user's design used, how many lines changed, and why not."""
        return {
            "own": self.pdf is not None,
            "kind": self.kind,
            "changed": len(self.applied),
            "kept": len(self.skipped),
            "source": self.suffix if self.source is not None else "",
            "problems": self.problems[:3],
        }


def contacts_kept(pdf: bytes, expected: Contact) -> list[str]:
    """The contact details ``pdf`` no longer shows (empty when it shows all of them)."""
    text, _ = pdf_text(pdf)
    return contact_problems(expected, text, pdf_links(pdf))


def own_design(original: Original, edits: list[Edit], contact: Contact) -> OwnDesign:
    """The user's file with ``edits`` written in; ``contact`` must survive in the PDF."""
    if original.kind == "pdf":
        edited = edit_pdf(original.data, edits)
        result = OwnDesign(edited.pdf, applied=edited.applied, skipped=edited.skipped)
    elif original.kind == "docx":
        word = edit_docx(original.data, edits)
        result = OwnDesign(docx_to_pdf(word.docx), word.docx, ".docx", word.applied, word.skipped)
        if result.pdf is None:
            result.problems.append(
                "LibreOffice is not installed here, so your Word file could not become a PDF"
            )
    else:
        source = edit_tex(original.data.decode("utf-8", errors="replace"), edits)
        result = OwnDesign(
            compile_tex(source.source), source.source.encode("utf-8"), ".tex", source.applied, source.skipped
        )
        if result.pdf is None:
            result.problems.append("no LaTeX engine (Tectonic) could compile your .tex here")
    result.kind = original.kind
    if result.pdf is not None:
        missing = contacts_kept(result.pdf, contact)
        if missing:
            result.problems.append(f"it would drop {', '.join(missing)}")
            result.pdf = None
    return result
