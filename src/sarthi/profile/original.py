"""The user's own resume file, kept so tailored resumes can be made in its design.

Sarthi reads an upload into text (``profile.resume``) for matching and the master resume. The file itself is
kept too (``data/resume/original.<pdf|docx|tex>``, readable only by the user) so a tailored resume changes
only the words in the user's own layout, fonts and links. A note beside it records which resume text it was
read into: when the profile's resume text changes (a newer upload, a cancelled setup) the file no longer
matches and is not used.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from sarthi.profile.latex import is_latex
from sarthi.profile.resume import ResumeKind, detect

ORIGINAL_DIR = "resume"
_NOTE = "original.json"
SUFFIXES = {"pdf": ".pdf", "docx": ".docx", "tex": ".tex"}
_PRIVATE = 0o600


def text_hash(text: str) -> str:
    """Identifies one resume text (the master resume and the kept file both refer to it)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Original:
    """A kept upload: its bytes and kind ("pdf", "docx" or "tex")."""

    kind: str
    data: bytes

    @property
    def suffix(self) -> str:
        """The file extension for this kind."""
        return SUFFIXES[self.kind]


def kind_of(data: bytes) -> str:
    """The upload's kind: pdf, docx, tex, or "" for plain text (nothing to keep: it has no design)."""
    kind = detect(data)
    if kind is ResumeKind.PDF:
        return "pdf"
    if kind is ResumeKind.DOCX:
        return "docx"
    return "tex" if is_latex(data.decode("utf-8", errors="replace")) else ""


def _write_private(path: Path, content: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
    os.chmod(tmp, _PRIVATE)  # noqa: PTH101  (an existing file keeps its old mode on open)
    tmp.replace(path)


def keep_original(data_dir: Path, data: bytes, text: str) -> None:
    """Keep ``data`` (already read into ``text``) as the user's original; any older original is removed."""
    folder = data_dir / ORIGINAL_DIR
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        folder.chmod(0o700)
    for old in folder.glob("original.*"):
        old.unlink(missing_ok=True)
    kind = kind_of(data)
    if not kind:
        return
    _write_private(folder / f"original{SUFFIXES[kind]}", data)
    note = {"kind": kind, "text_hash": text_hash(text)}
    _write_private(folder / _NOTE, json.dumps(note).encode("utf-8"))


def load_original(data_dir: Path, text: str) -> Original | None:
    """The kept file when it is the one ``text`` was read from, else None."""
    folder = data_dir / ORIGINAL_DIR
    try:
        note = json.loads((folder / _NOTE).read_text(encoding="utf-8"))
        kind = str(note["kind"])
        if kind not in SUFFIXES or note.get("text_hash") != text_hash(text):
            return None
        return Original(kind, (folder / f"original{SUFFIXES[kind]}").read_bytes())
    except (OSError, ValueError, KeyError, TypeError):
        return None
