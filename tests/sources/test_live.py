"""Real network calls. Skipped by default; run with: uv run pytest -m live --no-cov"""

import pytest

from sarthi.jobs.normalize import complete
from sarthi.sources.ats import CompanyBoardSource
from sarthi.sources.base import SearchQuery
from sarthi.sources.boards import JobBoardSource
from sarthi.sources.feeds import FEEDS, FeedSource
from sarthi.sources.freehire import FreehireSource
from sarthi.sources.hackernews import HackerNewsSource

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


@pytest.mark.parametrize("feed", sorted(FEEDS))
async def test_public_feed(feed: str) -> None:
    postings = await FeedSource(feed).fetch(SearchQuery())
    assert len(postings) > 5, feed
    assert all(p.title and p.company for p in postings)


async def test_hacker_news_who_is_hiring() -> None:
    postings = await HackerNewsSource().fetch(SearchQuery())
    assert len(postings) > 20
    assert sum(1 for p in postings if p.title != "Open role (see the post)") > len(postings) // 2


async def test_freehire_india_remote() -> None:
    query = SearchQuery(roles=("data engineer",), countries=("IN",), remote=True, hours_old=72)
    postings = await FreehireSource().fetch(query)
    assert postings
    assert all("IN" in p.remote_countries for p in postings)
