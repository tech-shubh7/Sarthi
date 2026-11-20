"""Fill what a source left out, from the job's own location field and text, without guessing.

Sources differ: ATS-Scrapers gives country codes, JobSpy gives a city string, a Telegram post gives
prose.
``complete`` brings them all to the same level using the geo detector. A value a source provided always
wins.
"""

from __future__ import annotations

from typing import Any

from sarthi.jobs import geo
from sarthi.jobs.models import (
    DESCRIPTION_SNIPPET_CHARS,
    EmploymentType,
    JobPosting,
    RemoteType,
    Seniority,
    Sponsorship,
)

_UNKNOWN = "unknown"


def snippet(text: str, limit: int = DESCRIPTION_SNIPPET_CHARS) -> tuple[str, bool]:
    """Return ``(text, complete)``: the whole text if it fits, else a word-boundary cut with an ellipsis."""
    text = text.strip()
    if len(text) <= limit:
        return text, True
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return f"{cut}…", False


def complete(job: JobPosting) -> JobPosting:
    """Return ``job`` with unknown location, remote, visa, contract and seniority filled from its text."""
    found = geo.detect(job.title, f"{job.location_text}\n{job.description}")
    updates: dict[str, Any] = {}

    if not job.country_codes:
        codes = geo.location_codes(job.location_text) or ({found["country"]} if "country" in found else set())
        if codes:
            updates["country_codes"] = frozenset(codes)

    if not job.location_text and "location_text" in found:
        updates["location_text"] = found["location_text"]

    defaults: list[tuple[str, str, type[Any]]] = [
        ("remote_type", "arrangement", RemoteType),
        ("visa_sponsorship", "sponsorship", Sponsorship),
        ("relocation", "relocation", Sponsorship),
        ("employment_type", "employment_type", EmploymentType),
        ("seniority", "seniority", Seniority),
    ]
    for field, key, enum in defaults:
        if getattr(job, field) == _UNKNOWN and key in found:
            updates[field] = enum(found[key])

    remote_type = updates.get("remote_type", job.remote_type)
    if remote_type is RemoteType.REMOTE and not job.remote_countries:
        # A remote job located in a country (e.g. "Remote - India") is open to that country, not the world.
        located_in = updates.get("country_codes", job.country_codes)
        if located_in:
            updates["remote_countries"] = located_in

    if not updates:
        return job
    # Re-validate rather than model_copy(update=...), which would skip validation of the new values.
    return JobPosting.model_validate({**job.model_dump(), **updates})
