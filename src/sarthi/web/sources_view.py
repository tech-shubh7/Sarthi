"""The Sources page: every source Sarthi reads, the sites their jobs came from, and company career pages.

A job remembers the site it was first found on (``job.source``), so the page lists those sites and credits
each to the source that reads it. Which source reads which site comes from ``sarthi.sources.sites`` through
a ``SiteMap`` the composition root builds (the dashboard does not import the pipeline steps).
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sarthi.db.dashboard import DashboardQueries, SiteRow

RECENT = timedelta(days=7)


def _no_reader(site: str, names: Collection[str]) -> str | None:
    return None


@dataclass(frozen=True, slots=True)
class SiteMap:
    """What the sources know: career-page hiring systems, who reads a site, the snapshot's name."""

    career: tuple[str, ...] = ()
    reader: Callable[[str, Collection[str]], str | None] = _no_reader
    catalog: str = ""


@dataclass(frozen=True, slots=True)
class SiteLine:
    """One site on the page, with the source that reads it."""

    row: SiteRow
    reader: str | None
    career: bool  # a hiring system of companies' own career pages


def sources_view(
    queries: DashboardQueries, now: datetime, limit: int, offset: int, sites_map: SiteMap
) -> dict[str, Any]:
    """The Sources page's data; ``limit`` and ``offset`` pick one page of the company career pages."""
    rows = queries.sources()
    names = {r.name for r in rows}
    systems = set(sites_map.career)
    sites = [
        SiteLine(s, sites_map.reader(s.site, names), s.site in systems) for s in queries.sites(now - RECENT)
    ]
    reader_jobs: dict[str, int] = {}
    for line in sites:
        if line.reader is not None:
            reader_jobs[line.reader] = reader_jobs.get(line.reader, 0) + line.row.jobs
    career_sites = [line.row for line in sites if line.career]
    return {
        "rows": rows,
        "reader_jobs": reader_jobs,
        "sites": sites,
        "site_jobs": sum(line.row.jobs for line in sites),
        "systems": career_sites,
        "supported_systems": sorted(systems),
        "career_jobs": sum(s.jobs for s in career_sites),
        "career": queries.career_pages(sorted(systems), limit, offset=offset),
        "catalog": next((r for r in rows if r.name == sites_map.catalog), None),
        "recent_days": RECENT.days,
    }
