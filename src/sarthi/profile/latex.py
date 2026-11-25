r"""Plain text from a LaTeX resume, and the source spans that print a given line.

LaTeX uploads are read without running TeX: comments are dropped, formatting commands keep their text,
layout commands (spacing, packages, definitions) disappear, and ``\\href`` targets are kept so profile links
are found. The same conversion is used to find which part of the source prints a resume line, so a tailored
line can be written back into the user's own source with every macro around it untouched.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

# Commands whose arguments are layout or definitions, never resume text.
_DROP_ARGS = frozenset(
    {
        "documentclass", "usepackage", "RequirePackage", "newcommand", "renewcommand", "providecommand",
        "newenvironment", "renewenvironment", "def", "setlength", "addtolength", "vspace", "hspace",
        "vskip", "hskip", "pagestyle", "thispagestyle", "definecolor", "colorlet", "titleformat",
        "titlespacing", "geometry", "hypersetup", "input", "include", "label", "ref", "pagenumbering",
        "fontsize", "linespread", "setmainfont", "setsansfont", "urlstyle", "color", "pdfgentounicode",
        "includegraphics", "rule", "raisebox", "makebox", "setlist", "newlength", "faicon", "begin", "end",
    }
)  # fmt: skip
# Commands whose single argument is printed inline (no line break).
_INLINE = frozenset(
    {
        "textbf", "textit", "emph", "underline", "textsc", "texttt", "textsf", "textrm", "textmd",
        "textup", "textsl", "uline", "mbox", "text", "textnormal", "scshape", "bfseries", "itshape",
        "small", "footnotesize", "large", "Large", "LARGE", "huge", "Huge", "normalsize", "tiny",
        "scriptsize", "sffamily", "rmfamily", "ttfamily", "mdseries", "upshape", "url", "textcolor",
    }
)  # fmt: skip
_ESCAPES = {"&": "&", "%": "%", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}", " ": " ", ",": " "}
_SYMBOLS = {"\\": "\n", "newline": "\n", "linebreak": "\n", "par": "\n\n", "item": "\n• ", "hfill": " ",
            "quad": " ", "qquad": " ", "enspace": " ", "LaTeX": "LaTeX", "TeX": "TeX", "ldots": "...",
            "textbar": "|", "textbullet": "•", "cdot": "·", "textendash": "-", "textemdash": "-"}  # fmt: skip
_LATEX_MARKERS = re.compile(r"\\documentclass|\\begin\{document\}|\\section\*?\{|\\resumeItem|\\item\b")
_COMMENT = re.compile(r"(?<!\\)%[^\n]*")


def is_latex(text: str) -> bool:
    """True when ``text`` looks like LaTeX source rather than a plain-text resume."""
    return len(_LATEX_MARKERS.findall(text[:20_000])) >= 2  # noqa: PLR2004  (two markers, not one stray \item)


def strip_comments(src: str) -> str:
    r"""``src`` without ``%`` comments (an escaped ``\\%`` stays)."""
    return _COMMENT.sub("", src)


def group_end(src: str, start: int) -> int:
    """The index just after the brace group that opens at ``start`` (a ``{``); -1 if unclosed."""
    depth = 0
    i = start
    while i < len(src):
        char = src[i]
        if char == "\\":
            i += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _skip_space(src: str, i: int) -> int:
    while i < len(src) and src[i] in " \t":
        i += 1
    return i


def _arguments(src: str, i: int) -> tuple[list[str], int]:
    """The ``[optional]`` and ``{brace}`` arguments after a command at ``i``, and the index after them."""
    args: list[str] = []
    while True:
        j = _skip_space(src, i)
        if j < len(src) and src[j] == "[":
            close = src.find("]", j)
            if close < 0:
                return args, i
            i = close + 1
            continue
        if j < len(src) and src[j] == "{":
            end = group_end(src, j)
            if end < 0:
                return args, i
            args.append(src[j + 1 : end - 1])
            i = end
            continue
        return args, i


def _text(src: str, links: list[str]) -> str:  # noqa: PLR0912  (one small state machine)
    out: list[str] = []
    i = 0
    while i < len(src):
        char = src[i]
        if char == "\\":
            match = re.match(r"\\([A-Za-z@]+\*?|.)", src[i:])
            if not match:
                break
            name = match.group(1).rstrip("*")
            i += match.end()
            if name in _ESCAPES:
                out.append(_ESCAPES[name])
                continue
            if name in _SYMBOLS:
                out.append(_SYMBOLS[name])
                if name == "item":
                    _, i = _arguments(src, i)  # \item[label]
                continue
            args, i = _arguments(src, i)
            if name in _DROP_ARGS:
                continue
            if name == "href" and len(args) >= 2:  # noqa: PLR2004
                target = args[0].replace("\\#", "#").replace("\\_", "_").replace("\\%", "%").strip()
                if target and target not in links:
                    links.append(target)
                out.append(_text(args[1], links))
            elif name == "textcolor" and len(args) >= 2:  # noqa: PLR2004
                out.append(_text(args[1], links))
            elif name in _INLINE:
                out.append(" ".join(_text(a, links) for a in args))
            elif args:
                out.append("\n" + "\n".join(_text(a, links) for a in args) + "\n")
            continue
        if char in "{}":
            i += 1
            continue
        if char == "~":
            out.append(" ")
        elif char == "&":
            out.append(" | ")
        elif char == "$":
            pass
        else:
            out.append(char)
        i += 1
    text = "".join(out)
    return text.replace("---", "-").replace("--", "-").replace("``", '"').replace("''", '"')


def body(src: str) -> str:
    r"""The part of the source between ``\\begin{document}`` and ``\\end{document}`` (all of it if absent)."""
    src = strip_comments(src)
    start = src.find("\\begin{document}")
    end = src.find("\\end{document}")
    if start >= 0:
        src = src[start + len("\\begin{document}") : end if end > start else len(src)]
    return src


def plain(fragment: str) -> str:
    """One source fragment as plain text, whitespace collapsed (used to match resume lines)."""
    return re.sub(r"\s+", " ", _text(strip_comments(fragment), [])).strip(" •|")


def latex_to_text(src: str) -> str:
    """The resume's readable text. Link targets that are not printed are added at the end."""
    links: list[str] = []
    text = _text(body(src), links)
    lines = [re.sub(r"[ \t]+", " ", line).strip(" |") for line in text.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    hidden = [link.removeprefix("mailto:") for link in links if link.removeprefix("mailto:") not in text]
    return text + ("\n" + " ".join(hidden) if hidden else "")


def links_in(src: str) -> list[str]:
    r"""Every ``\\href`` and ``\\url`` target in the source, in order."""
    found: list[str] = []
    clean = strip_comments(src)
    for match in re.finditer(r"\\(?:href|url)\s*\{", clean):
        end = group_end(clean, match.end() - 1)
        if end > 0:
            target = clean[match.end() : end - 1].strip()
            if target and target not in found:
                found.append(target)
    return found


# ---- where a line is printed --------------------------------------------------------------------------


def spans(src: str) -> Iterator[tuple[int, int]]:
    r"""Source ranges that may print one resume line: brace-group contents and ``\\item`` bodies."""
    start_body = src.find("\\begin{document}")
    offset = start_body if start_body >= 0 else 0
    for match in re.finditer(r"\{", src[offset:]):
        start = offset + match.start()
        if start > 0 and src[start - 1] == "\\":
            continue
        end = group_end(src, start)
        if end > 0:
            yield start + 1, end - 1
    stops = re.compile(r"\\item\b|\\end\s*\{|\\begin\s*\{|\\resumeItem|\n\s*\n")
    for match in re.finditer(r"\\item\b(?:\s*\[[^\]]*\])?", src[offset:]):
        start = offset + match.end()
        stop = stops.search(src, start)
        end = stop.start() if stop else len(src)
        yield start, end
    for match in re.finditer(r"(?:^|\n)[ \t]*\n((?:[^\n]+\n?)+)", src[offset:]):  # paragraphs
        yield offset + match.start(1), offset + match.end(1)


def escape(text: str) -> str:
    """Plain text made safe to print in LaTeX source."""
    special = {"\\": r"\textbackslash{}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(special.get(c, "\\" + c if c in "&%$#_{}" else c) for c in text)
