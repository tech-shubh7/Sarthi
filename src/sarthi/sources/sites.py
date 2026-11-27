"""Which source reads which site, and which sites are companies' own career pages (for the Sources page).

Sarthi reads a few sources, and most of them reach many sites: the job-board search covers LinkedIn, Indeed
and a country's own board (Naukri for India); Freehire covers ~430 job sites and hiring systems; ATS-Scrapers'
snapshot covers roughly 192,000 company career pages. The composition root hands this mapping to the
dashboard.
"""

from __future__ import annotations

from collections.abc import Collection
from functools import cache

from jobspy.model import Site

from sarthi.sources.aggregators import AdzunaSource, JoobleSource
from sarthi.sources.ats import supported_systems
from sarthi.sources.catalog import CatalogSource
from sarthi.sources.feeds import FEEDS
from sarthi.sources.freehire import FreehireSource
from sarthi.sources.hackernews import HackerNewsSource

CATALOG = CatalogSource.name
BOARDS = "boards:"
JOBSPY_SITES = frozenset(site.value for site in Site)
AGGREGATORS = frozenset(name.removeprefix(BOARDS) for name in (AdzunaSource.name, JoobleSource.name))
# Sites that are boards, feeds or searches, even where ATS-Scrapers also has a reader for them.
NOT_CAREER_PAGES = (
    JOBSPY_SITES
    | AGGREGATORS
    | set(FEEDS)
    | {HackerNewsSource.name.partition(":")[2], FreehireSource.name.partition(":")[2]}
)


@cache
def career_systems() -> list[str]:
    """Hiring systems whose jobs come from a company's own career page (Greenhouse, Lever, Keka, ...)."""
    return sorted(set(supported_systems()) - NOT_CAREER_PAGES)


def reader_of(site: str, names: Collection[str]) -> str | None:
    """The source among ``names`` that finds jobs on ``site``; None when none of them reads it now."""
    for name in (f"feed:{site}", f"search:{site}"):
        if name in names:
            return name
    boards = sorted(n for n in names if n.startswith(BOARDS))
    for name in boards:
        if site in name.removeprefix(BOARDS).split("+"):
            return name
    if site in JOBSPY_SITES:  # a country's own board (Naukri for India) is searched with the others
        return next((n for n in boards if n.removeprefix(BOARDS) not in AGGREGATORS), None)
    if site in career_systems():
        if CatalogSource.name in names:
            return CatalogSource.name
        return next((n for n in sorted(names) if n.startswith(f"{site}:")), None)  # a watched company
    return None
