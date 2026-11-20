"""Real network calls. Skipped by default; run with: uv run pytest -m live --no-cov"""

import pytest

from sarthi.jobs.normalize import complete
from sarthi.sources.ats import CompanyBoardSource
from sarthi.sources.base import SearchQuery
from sarthi.sources.boards import JobBoardSource

pytestmark = pytest.mark.live


async def test_greenhouse_board() -> None:
    postings = await CompanyBoardSource("greenhouse", "anthropic").fetch(SearchQuery())
    assert len(postings) > 10
    filled = [complete(p) for p in postings]
    assert sum(1 for p in filled if p.country_codes) > len(filled) // 2  # countries come from location text


async def test_linkedin_and_naukri_in_india_last_day() -> None:
    query = SearchQuery(roles=("backend engineer",), countries=("IN",), hours_old=24, results_per_search=5)
    postings = await JobBoardSource(("linkedin",)).fetch(query)
    assert postings
    assert {p.source for p in postings} <= {"linkedin", "naukri"}
    assert all(p.posted_at is None or p.posted_at.tzinfo for p in postings)
