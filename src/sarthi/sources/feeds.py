"""Free public job feeds: remote-job boards with an open API or RSS feed, no key, no scraping.

Connectors ported from StillOpen's ``scan.py`` (Apache-2.0): Remotive, RemoteOK, We Work Remotely,
Himalayas, Jobicy, Arbeitnow and Working Nomads. Each feed is one ``FeedSource``; a feed lists everything it
has and matching filters later, so one read is one HTTP request. Where a remote job may be done from is read
from the feed's own location field ("USA only", "Anywhere in the World"), never guessed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from defusedxml import ElementTree
from pydantic import ValidationError

from sarthi.clock import Clock, utcnow
from sarthi.jobs.models import EmploymentType, JobPosting, RemoteType, Salary, SalaryPeriod
from sarthi.queue.tasks import RetryableError
from sarthi.sources import web
from sarthi.sources.base import SearchQuery

log = logging.getLogger(__name__)

Row = dict[str, Any]


def _rows(data: Any, key: str | None) -> list[Row]:
    items = data.get(key) if key and isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RetryableError(f"feed answered in an unexpected shape (no {key or 'list'})")
    return [item for item in items if isinstance(item, dict)]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _employment(value: Any) -> EmploymentType:
    text = " ".join(value) if isinstance(value, list) else _text(value)
    text = text.lower().replace("-", " ").replace("_", " ")
    for words, kind in (
        (("intern",), EmploymentType.INTERNSHIP),
        (("contract", "temporary", "freelance"), EmploymentType.CONTRACT),
        (("part time",), EmploymentType.PART_TIME),
        (("full time", "permanent"), EmploymentType.FULL_TIME),
    ):
        if any(word in text for word in words):
            return kind
    return EmploymentType.UNKNOWN


_PERIODS = {
    "annual": SalaryPeriod.YEAR,
    "yearly": SalaryPeriod.YEAR,
    "year": SalaryPeriod.YEAR,
    "monthly": SalaryPeriod.MONTH,
    "month": SalaryPeriod.MONTH,
    "weekly": SalaryPeriod.WEEK,
    "daily": SalaryPeriod.DAY,
    "hourly": SalaryPeriod.HOUR,
    "hour": SalaryPeriod.HOUR,
}


def _salary(currency: Any, low: Any, high: Any, period: Any = "year") -> Salary | None:
    try:
        minimum = float(low) if low not in (None, "", 0, "0") else None
        maximum = float(high) if high not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return None
    code = _text(currency).upper()
    if (minimum is None and maximum is None) or len(code) != 3:  # noqa: PLR2004  (ISO 4217)
        return None
    try:
        return Salary(
            currency=code,
            period=_PERIODS.get(_text(period).lower(), SalaryPeriod.YEAR),
            minimum=minimum,
            maximum=maximum,
        )
    except ValidationError:
        return None


# ---- one parser per feed: answer -> dicts with JobPosting fields ----


def _remotive(data: Any) -> list[Row]:
    return [
        {
            "source_job_id": str(j.get("id") or "") or None,
            "url": j.get("url"),
            "title": j.get("title"),
            "company": j.get("company_name"),
            "location_text": _text(j.get("candidate_required_location")) or "Worldwide",
            "employment_type": _employment(j.get("job_type")),
            "description": web.plain_text(j.get("description")),
            "posted_at": web.parse_time(j.get("publication_date")),
        }
        for j in _rows(data, "jobs")
    ]


def _remoteok(data: Any) -> list[Row]:
    return [
        {
            "source_job_id": str(j["id"]),
            "url": j.get("url"),
            "apply_url": j.get("apply_url"),
            "title": j.get("position"),
            "company": j.get("company"),
            "location_text": _text(j.get("location")) or "Worldwide",
            "salary": _salary("USD", j.get("salary_min"), j.get("salary_max")),
            "description": web.plain_text(j.get("description")),
            "posted_at": web.parse_time(j.get("epoch") or j.get("date")),
        }
        for j in _rows(data, None)
        if j.get("id")  # the first element is RemoteOK's legal notice, not a job
    ]


def _himalayas(data: Any) -> list[Row]:
    rows = []
    for j in _rows(data, "jobs"):
        places = j.get("locationRestrictions") or []
        rows.append(
            {
                "source_job_id": _text(j.get("guid")) or None,
                "url": j.get("applicationLink") or j.get("guid"),
                "title": j.get("title"),
                "company": j.get("companyName"),
                "location_text": ", ".join(str(p) for p in places if p) or "Worldwide",
                "employment_type": _employment(j.get("employmentType")),
                "salary": _salary(
                    j.get("currency"), j.get("minSalary"), j.get("maxSalary"), j.get("salaryPeriod")
                ),
                "description": web.plain_text(j.get("description") or j.get("excerpt")),
                "posted_at": web.parse_time(j.get("pubDate")),
            }
        )
    return rows


def _jobicy(data: Any) -> list[Row]:
    return [
        {
            "source_job_id": str(j.get("id") or "") or None,
            "url": j.get("url"),
            "title": j.get("jobTitle"),
            "company": j.get("companyName"),
            "location_text": _text(j.get("jobGeo")) or "Worldwide",
            "employment_type": _employment(j.get("jobType")),
            "salary": _salary(
                j.get("salaryCurrency"), j.get("salaryMin"), j.get("salaryMax"), j.get("salaryPeriod")
            ),
            "description": web.plain_text(j.get("jobDescription") or j.get("jobExcerpt")),
            "posted_at": web.parse_time(j.get("pubDate")),
        }
        for j in _rows(data, "jobs")
    ]


def _arbeitnow(data: Any) -> list[Row]:
    rows = []
    for j in _rows(data, "data"):
        remote = j.get("remote") is True
        rows.append(
            {
                "source_job_id": _text(j.get("slug")) or None,
                "url": j.get("url"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "location_text": _text(j.get("location")) or ("Germany" if not remote else ""),
                "remote_type": RemoteType.REMOTE if remote else RemoteType.UNKNOWN,
                "employment_type": _employment(j.get("job_types")),
                "description": web.plain_text(j.get("description")),
                "posted_at": web.parse_time(j.get("created_at")),
            }
        )
    return rows


def _workingnomads(data: Any) -> list[Row]:
    return [
        {
            "url": j.get("url"),
            "title": j.get("title"),
            "company": j.get("company_name"),
            "location_text": _text(j.get("location")) or "Worldwide",
            "description": web.plain_text(j.get("description")),
            "posted_at": web.parse_time(j.get("pub_date")),
        }
        for j in _rows(data, None)
    ]


def _weworkremotely(xml_text: str) -> list[Row]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise RetryableError("weworkremotely: feed is not valid RSS") from error
    rows = []
    for item in root.iter("item"):
        company, sep, role = (item.findtext("title") or "").strip().partition(": ")
        if not sep:
            company, role = "", company
        rows.append(
            {
                "source_job_id": (item.findtext("guid") or "").strip() or None,
                "url": (item.findtext("link") or "").strip(),
                "title": role,
                "company": company,
                "location_text": (item.findtext("region") or "").strip() or "Worldwide",
                "employment_type": _employment(item.findtext("type")),
                "description": web.plain_text(item.findtext("description")),
                "posted_at": web.parse_time(item.findtext("pubDate")),
            }
        )
    return rows


@dataclass(frozen=True, slots=True)
class Feed:
    """Where a feed lives and how its answer is read."""

    url: str
    parse: Callable[[Any], list[Row]]
    xml: bool = False
    all_remote: bool = True  # every job on the board is remote


FEEDS: dict[str, Feed] = {
    "remotive": Feed("https://remotive.com/api/remote-jobs", _remotive),
    "remoteok": Feed("https://remoteok.com/api", _remoteok),
    "weworkremotely": Feed("https://weworkremotely.com/remote-jobs.rss", _weworkremotely, xml=True),
    "himalayas": Feed("https://himalayas.app/jobs/api?limit=100", _himalayas),
    "jobicy": Feed("https://jobicy.com/api/v2/remote-jobs?count=100", _jobicy),
    "arbeitnow": Feed("https://www.arbeitnow.com/api/job-board-api", _arbeitnow, all_remote=False),
    "workingnomads": Feed("https://www.workingnomads.com/api/exposed_jobs/", _workingnomads),
}


def to_posting(feed_name: str, feed: Feed, row: Row) -> JobPosting:
    """One feed row as a posting. Raises ``ValidationError`` for rows without a title, company or link."""
    data = {
        k: web.fix_mojibake(v) if isinstance(v, str) else v for k, v in row.items() if v not in (None, "")
    }
    if feed.all_remote:
        data["remote_type"] = RemoteType.REMOTE
    data["source"] = feed_name
    data["location_text"] = str(data.get("location_text", ""))[:300]
    return JobPosting.model_validate(data)


class FeedSource:
    """Every job on one public feed."""

    def __init__(
        self, feed: str, *, transport: httpx.AsyncBaseTransport | None = None, clock: Clock = utcnow
    ) -> None:
        """Read the feed named ``feed`` (a key of ``FEEDS``). Raises ``KeyError`` for unknown feeds."""
        self.feed_name = feed
        self.feed = FEEDS[feed]
        self._transport = transport
        self._clock = clock

    @property
    def name(self) -> str:
        """``"feed:<name>"``."""
        return f"feed:{self.feed_name}"

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """Every job on the feed, limited to ``query.hours_old`` when the feed dates its jobs."""
        async with web.client(self._transport) as http:
            response = await web.fetch(http, self.name, self.feed.url)
        if self.feed.xml:
            rows = self.feed.parse(response.text)
        else:
            try:
                rows = self.feed.parse(response.json())
            except ValueError as error:
                raise RetryableError(f"{self.name}: answer is not JSON") from error
        postings: list[JobPosting] = []
        for row in rows:
            try:
                postings.append(to_posting(self.feed_name, self.feed, row))
            except ValidationError as error:
                log.debug("%s: skipped unusable job: %d problem(s)", self.name, error.error_count())
        if query.hours_old is None:
            return postings
        since = self._clock() - timedelta(hours=query.hours_old)
        return [p for p in postings if p.posted_at is None or p.posted_at >= since]
