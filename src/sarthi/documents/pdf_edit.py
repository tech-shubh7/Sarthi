"""Tailored lines written into the user's own PDF, in place, with PyMuPDF (AGPL-3.0).

Only the text of a changed line moves: the old words are removed (redaction that keeps images, rules and
backgrounds) and the new words are written at the same baselines, left edges, size and colour, in the font
the PDF already embeds when it has every letter needed (else a standard font of the same style). A line is
changed only when the new words fit in the lines the old ones used; otherwise it keeps its original words.
The header (name, contact details, clickable links) is never touched.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Any

from sarthi.documents.build import Edit

# PyMuPDF ships without type hints: used as an untyped module.
pymupdf: Any = importlib.import_module("pymupdf")

_MARKERS = frozenset("•●○◦▪■‣-\u2013*·➢➤►✓")
_MAX_LINES = 8
_SAME_LEFT = 3.0  # points: lines starting this close to each other are in one column
_REDACT_INSET = 0.2  # share of a line's height left out of the redaction, so the lines above and below stay


@dataclass
class PdfResult:
    """The edited PDF and which edits were made."""

    pdf: bytes
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class _Line:
    spans: list[dict[str, Any]]

    @property
    def text(self) -> str:
        return "".join(s["text"] for s in self.spans)


def norm(text: str) -> str:
    """Text compared the way a reader sees it: one space, no soft hyphens, no bullet marker."""
    text = text.replace("\xad", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip().lstrip("".join(_MARKERS)).strip().lower()


def _page_lines(page: Any) -> list[_Line]:
    lines: list[_Line] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip() and s["text"].strip() not in _MARKERS]
            if spans:
                lines.append(_Line(spans))
    lines.sort(key=lambda line: (round(line.spans[0]["origin"][1], 1), line.spans[0]["origin"][0]))
    return lines


def _find(lines: list[_Line], target: str) -> list[_Line] | None:
    """The run of consecutive lines whose text is ``target``."""
    for i in range(len(lines)):
        acc = ""
        for j in range(i, min(i + _MAX_LINES, len(lines))):
            acc = norm(f"{acc} {lines[j].text}")
            if acc == target:
                return lines[i : j + 1]
            if not target.startswith(acc):
                break
    return None


def _wrap(font: Any, text: str, size: float, widths: list[float]) -> list[str] | None:
    """``text`` broken into lines no wider than ``widths``; None when it needs more lines than that."""
    out: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if font.text_length(trial, size) <= widths[min(len(out), len(widths) - 1)] + 0.5:
            current = trial
            continue
        if not current:
            return None  # one word wider than the line
        out.append(current)
        current = word
    out.append(current)
    return out if len(out) <= len(widths) else None


class _Fonts:
    """The fonts to write with: the PDF's own embedded font when it has every glyph, else a standard one."""

    def __init__(self, doc: Any) -> None:
        self.doc = doc
        self._cache: dict[int, tuple[Any, bytes] | None] = {}

    def _embedded(self, page: Any, name: str) -> tuple[int, Any, bytes] | None:
        for xref, _ext, _type, basefont, *_ in page.get_fonts():
            if basefont.split("+")[-1] != name:
                continue
            if xref not in self._cache:
                try:
                    buffer = self.doc.extract_font(xref)[3]
                    self._cache[xref] = (pymupdf.Font(fontbuffer=buffer), buffer) if buffer else None
                except (RuntimeError, ValueError):
                    self._cache[xref] = None
            found = self._cache[xref]
            return (xref, *found) if found else None
        return None

    def pick(self, page: Any, span: dict[str, Any], text: str) -> tuple[str, Any, bytes | None]:
        """Name, font and file (None for a standard font) to write ``text`` in ``span``'s style."""
        own = self._embedded(page, span["font"])
        if own is not None:
            xref, font, buffer = own
            if all(font.has_glyph(ord(c)) for c in text if not c.isspace()):
                return f"SarthiF{xref}", font, buffer
        bold, italic = bool(span["flags"] & 16), bool(span["flags"] & 2)
        serif = bool(span["flags"] & 4)
        family = ("ti" if serif else "he") if not span["font"].lower().startswith("cour") else "co"
        style = {(False, False): "ro", (True, False): "bo", (False, True): "it", (True, True): "bi"}
        code = (
            {"ti": "tiro", "he": "helv", "co": "cour"}[family]
            if (bold, italic) == (False, False)
            else (family + style[(bold, italic)])
        )
        return code, pymupdf.Font(code), None


def _fix_tounicode(doc: Any) -> None:
    r"""Readers must see a space and a hyphen, not U+00A0 / U+00AD, where an embedded font shares the glyph.

    A subset font often maps one glyph to both characters; the text map written for re-used fonts then
    names the wrong one, and a tracking system would read "zero\\xadresult".
    """
    seen: set[int] = set()
    for page in doc:
        for xref, _ext, _type, _base, name, *_ in page.get_fonts():
            if not str(name).startswith("SarthiF") or xref in seen:
                continue
            seen.add(xref)
            kind, value = doc.xref_get_key(xref, "ToUnicode")
            if kind != "xref":
                continue
            target = int(value.split()[0])
            stream = doc.xref_stream(target)
            fixed = re.sub(rb"<00[aA]0>", b"<0020>", stream)
            fixed = re.sub(rb"<00[aA][dD]>", b"<002D>", fixed)
            if fixed != stream:
                doc.update_stream(target, fixed)


def edit_pdf(data: bytes, edits: list[Edit]) -> PdfResult:
    """The PDF with each edit's text replaced where it fits; edits that do not fit keep the original."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    fonts = _Fonts(doc)
    result = PdfResult(b"")
    for edit in edits:
        if _apply(doc, fonts, edit):
            result.applied.append(edit.where)
        else:
            result.skipped.append(edit.where)
    _fix_tounicode(doc)
    result.pdf = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return result


def _apply(doc: Any, fonts: _Fonts, edit: Edit) -> bool:
    target = norm(edit.old)
    new = re.sub(r"\s+", " ", edit.new).strip()
    if not target or not new:
        return False
    for page in doc:
        lines = _page_lines(page)
        found = _find(lines, target)
        if found is None:
            continue
        first = found[0].spans[0]
        name, font, buffer = fonts.pick(page, first, new)
        left = first["origin"][0]  # the column's right edge: the widest line starting where this one does
        column = [line for line in lines if abs(line.spans[0]["origin"][0] - left) < _SAME_LEFT]
        right = max(s["bbox"][2] for line in column for s in line.spans)
        widths = [
            max(right, *(s["bbox"][2] for s in line.spans)) - line.spans[0]["origin"][0] for line in found
        ]
        wrapped = _wrap(font, new, first["size"], widths)
        if wrapped is None:
            return False
        for line in found:
            for span in line.spans:
                x0, y0, x1, y1 = span["bbox"]
                inset = (y1 - y0) * _REDACT_INSET
                page.add_redact_annot(pymupdf.Rect(x0, y0 + inset, x1, y1 - inset), fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        if buffer is not None:  # after the redaction, which tidies the page's resources
            page.insert_font(fontname=name, fontbuffer=buffer)
        color = first["color"]
        rgb = ((color >> 16) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255)
        for line, text in zip(found, wrapped, strict=False):
            page.insert_text(line.spans[0]["origin"], text, fontname=name, fontsize=first["size"], color=rgb)
        return True
    return False
