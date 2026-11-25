r"""Tailored lines written into the user's own LaTeX source, and the source compiled when a TeX engine exists.

Only the text of a changed line is replaced: the macro around it (``\\resumeItem{...}``, ``\\item``), every
other macro, the header with its ``\\href`` links and all spacing stay byte for byte. Words the original line
set in bold or italics stay so when the new wording still contains them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sarthi.documents.build import Edit
from sarthi.profile import latex

COMPILE_TIMEOUT_S = 180
_STYLED = re.compile(r"\\(textbf|textit|emph|underline|textsc)\s*\{")


@dataclass
class TexResult:
    """The edited source and which edits were made."""

    source: str
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _styled_words(fragment: str) -> list[tuple[str, str]]:
    """(macro, printed text) for each bold/italic group in ``fragment``."""
    found = []
    for match in _STYLED.finditer(fragment):
        end = latex.group_end(fragment, match.end() - 1)
        if end > 0:
            words = latex.plain(fragment[match.end() : end - 1])
            if words:
                found.append((match.group(1), words))
    return found


def _replacement(fragment: str, new: str) -> str:
    """``new`` as LaTeX, with the original's bold/italic words kept bold/italic, and its outer spacing."""
    body = latex.escape(new)
    for macro, words in _styled_words(fragment):
        escaped = latex.escape(words)
        at = body.find(escaped)
        if at >= 0 and f"\\{macro}{{{escaped}}}" not in body:
            body = f"{body[:at]}\\{macro}{{{escaped}}}{body[at + len(escaped) :]}"
    lead = re.match(r"[\s:;,|]*", fragment)  # "{: Python, SQL}" keeps its ": "
    trail = re.search(r"\s*$", fragment)
    return f"{lead.group(0) if lead else ''}{body}{trail.group(0) if trail else ''}"


def _span_for(src: str, old: str) -> tuple[int, int] | None:
    """The smallest source range that prints exactly ``old``."""
    target = _key(old)
    best: tuple[int, int] | None = None
    for start, end in latex.spans(src):
        size = end - start
        if size < len(old) * 0.7 or size > len(old) * 4 + 400 or (best and size >= best[1] - best[0]):
            continue
        if _key(latex.plain(src[start:end])) == target:
            best = (start, end)
    return best


def edit_tex(src: str, edits: list[Edit]) -> TexResult:
    """``src`` with each edit's line re-worded in place; an edit whose line cannot be found is skipped."""
    result = TexResult(src)
    for edit in edits:
        span = _span_for(result.source, edit.old)
        if span is not None:
            start, end = span
            fragment = result.source[start:end]
            result.source = result.source[:start] + _replacement(fragment, edit.new) + result.source[end:]
            result.applied.append(edit.where)
            continue
        literal = latex.escape(edit.old)
        if result.source.count(literal) == 1:  # a plain run of text, e.g. a skills list after its label
            result.source = result.source.replace(literal, latex.escape(edit.new))
            result.applied.append(edit.where)
            continue
        result.skipped.append(edit.where)
    return result


def engine() -> str | None:
    """The TeX engine on this computer: Tectonic (preferred, fetches packages itself) or a TeX Live one."""
    for name in ("tectonic", "xelatex", "pdflatex", "lualatex"):
        path = shutil.which(name)
        if path:
            return path
    return None


def compile_tex(src: str) -> bytes | None:
    """The source compiled to PDF; None when no engine is installed or it fails (missing files, packages)."""
    program = engine()
    if program is None:
        return None
    with tempfile.TemporaryDirectory(prefix="sarthi-tex-") as tmp:
        folder = Path(tmp)
        (folder / "resume.tex").write_text(src, encoding="utf-8")
        if Path(program).name == "tectonic":
            command = [program, "--untrusted", "--chatter", "minimal", "--outdir", tmp, "resume.tex"]
        else:
            command = [
                program,
                "-no-shell-escape",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "resume.tex",
            ]
        try:
            subprocess.run(  # noqa: S603  (fixed arguments, no shell)
                command, cwd=tmp, capture_output=True, timeout=COMPILE_TIMEOUT_S, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        pdf = folder / "resume.pdf"
        return pdf.read_bytes() if pdf.is_file() else None
