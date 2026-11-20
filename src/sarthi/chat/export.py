"""Matched jobs as a spreadsheet file (CSV) the user can open in Excel, Google Sheets or LibreOffice.

Every cell comes from job postings written by strangers, so a value that a spreadsheet would run as a formula
(starting with ``=``, ``+``, ``-``, ``@``, tab or carriage return) is prefixed with a quote (OWASP "CSV
injection"). The file starts with a UTF-8 BOM so Excel shows non-English text correctly.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass

from sarthi.chat.cards import display_company, place_text, salary_text
from sarthi.db.matches import MatchRecord
from sarthi.jobs.models import JobPosting

HEADER = (
    "Match %",
    "Fit",
    "Role",
    "Company",
    "Location",
    "Salary",
    "Posted",
    "Why it fits",
    "Check first",
    "Apply link",
)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_CELL_CHARS = 1000


@dataclass(frozen=True, slots=True)
class Row:
    """One matched job for the export."""

    job: JobPosting
    match: MatchRecord


def safe_cell(value: str) -> str:
    """A cell value that spreadsheets show as text, never run as a formula."""
    value = value.replace("\x00", "")[:_CELL_CHARS]
    return "'" + value if value.startswith(_FORMULA_START) else value


def matches_csv(rows: Iterable[Row]) -> bytes:
    """The CSV file's bytes, one line per job, in the order given."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    for row in rows:
        job, match = row.job, row.match
        cells = (
            str(match.score),
            "Strong" if match.decision == "alert" else "Fair",
            job.title,
            display_company(job.company),
            place_text(job),
            salary_text(job.salary) or "",
            job.posted_at.date().isoformat() if job.posted_at else "",
            "; ".join(match.reasons),
            "; ".join(match.notes),
            str(job.apply_url or job.url),
        )
        writer.writerow([safe_cell(cell) for cell in cells])
    return ("﻿" + out.getvalue()).encode("utf-8")
