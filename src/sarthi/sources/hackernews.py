"""Hacker News "Ask HN: Who is hiring?": the monthly thread where startups post jobs, one comment each.

Read through the public Algolia HN Search API (no key), the API Hacker News links for search. Ported from
StillOpen's ``fetch_hnhiring`` / ``parse_hn_comment`` (Apache-2.0): only the current and previous month's
threads are read, every comment is untrusted text reduced to plain words, and company, role and place are
taken from the first line (``Company | Role | Place | REMOTE``) only when they plainly look like one.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from sarthi.clock import Clock, utcnow
from sarthi.jobs.models import JobPosting
from sarthi.sources import web
from sarthi.sources.base import SearchQuery

log = logging.getLogger(__name__)

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
MAX_POSTINGS = 500
COMMENT_CHARS = 6000
OPEN_ROLE = "Open role (see the post)"

_STORY_TITLE = re.compile(r"^ask hn: who is hiring\?", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_HREF = re.compile(r"""href\s*=\s*["']([^"']{1,500})["']""", re.I)
_APPLY_URL = re.compile(
    r"/(jobs?|careers?|apply|hiring|join|openings?|positions?)\b|lever\.co|greenhouse\.io|ashbyhq\.com|"
    r"workable\.com|recruitee\.com|smartrecruiters\.com|wellfound\.com|workatastartup\.com",
    re.I,
)
_ROLE = re.compile(
    r"\b(engineer|engineers|developer|developers|designer|manager|scientist|analyst|architect|devops|sre|"
    r"founding|cto|researcher|recruiter|writer|marketer|marketing|sales|product|support|operations|security|"
    r"lead|director|head of|intern|programmer|administrator|consultant|specialist|ml|ai)\b",
    re.I,
)
_PLACE_WORDS = re.compile(r"\bremote\b|on-?site|\bhybrid\b", re.I)
_NOISE = re.compile(
    r"full[- ]?time|part[- ]?time|\bcontract|\bintern(ship)?s?\b|\bvisa\b|equity|[$€£]\s?\d|\bsalary\b|"
    r"\d\s?k\b|\bseries [a-z]\b|\bseed\b|\bstaff\b|\bpeople\b|employees",
    re.I,
)
_SITE = re.compile(r"(?:https?://)?[\w.-]+\.[a-z]{2,}(?:/\S*)?", re.I)
# Job seekers post in the hiring thread too (against its rules); seen live 2026-11-27, scored as jobs.
_SEEKER = re.compile(
    r"^\W*(?:(?:hi|hello|hey)\b[\s,!.-]*(?:everyone|all|hn|there)?[\s,!.-]*)?(?:i'?m|i\s+am|my\s+name\s+is)\b"
    r"|\b(?:i'?m|i\s+am)\s+(?:currently\s+|actively\s+)?(?:looking|seeking|open)\s+(?:for|to)\b"
    r"|\bopen\s+to\s+work\b|\bavailable\s+for\s+hire\b",
    re.I,
)
_EMPLOYER = re.compile(r"\bhiring\b|\bwe(?:'re|\s+are)\b|\bour\s+team\b|\bjoin\s+us\b", re.I)
_SHORT_PART_CHARS = 50
_SHORT_PART_WORDS = 6
_MIN_PARTS = 2


def _apply_link(raw: str) -> str | None:
    """The first link in the comment that looks like a jobs page (emails are left in the text)."""
    for href in _HREF.findall(raw or ""):
        link = web.plain_text(href).strip()
        if link.startswith(("http://", "https://")) and _APPLY_URL.search(link) and " " not in link:
            return link
    return None


def parse_comment(raw: str) -> dict[str, Any] | None:
    """One top-level comment as posting fields, or None when it does not read like a job post."""
    plain = web.plain_text(raw, COMMENT_CHARS)
    if not plain:
        return None
    header = plain.split("\n", 1)[0]
    if _SEEKER.search(header) and not _EMPLOYER.search(header):
        return None  # a person offering themselves ("I'm Asha, a full-stack engineer..."), not a job
    parts = [p.strip() for p in header.split("|") if p.strip()]
    if len(parts) < _MIN_PARTS and not _ROLE.search(header):
        return None
    company = re.sub(r"\s*\(\s*https?://[^)]*\)", "", parts[0] if len(parts) > 1 else "").strip()[:120]
    if company and " " not in company and "." in company and _SITE.fullmatch(company):
        company = company.split("//")[-1].split("/")[0].removeprefix("www.").split(".")[0].capitalize()
    roles: list[str] = []
    places: list[str] = []
    for part in parts[1:] if len(parts) > 1 else parts:
        if _PLACE_WORDS.search(part):
            places.append(part[:60])
        elif _SITE.fullmatch(part) or _NOISE.search(part):
            continue
        elif _ROLE.search(part):
            roles.append(part[:120])
        elif len(part) <= _SHORT_PART_CHARS and len(part.split()) <= _SHORT_PART_WORDS:
            places.append(part)
    return {
        "title": (" / ".join(roles[:3]) or OPEN_ROLE)[:200],
        "company": company or "A startup on Hacker News",
        "location_text": ", ".join(places[:3])[:160],
        "description": plain,
        "apply_url": _apply_link(raw),
    }


def _month_start(moment: datetime) -> datetime:
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class HackerNewsSource:
    """Startup jobs from this month's and last month's "Who is hiring?" threads."""

    name = "feed:hackernews"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, clock: Clock = utcnow) -> None:
        """``transport`` is injectable for tests."""
        self._transport = transport
        self._clock = clock

    async def fetch(self, query: SearchQuery) -> list[JobPosting]:
        """Every job comment in the recent threads (newer than ``query.hours_old`` when set)."""
        now = self._clock()
        threads_since = _month_start(_month_start(now) - timedelta(days=1))  # this month's and last month's
        since = threads_since
        if query.hours_old is not None:
            since = max(since, now - timedelta(hours=query.hours_old))
        async with web.client(self._transport) as http:
            found = await web.fetch_json(
                http, self.name, SEARCH_URL, tags="story,author_whoishiring", hitsPerPage="6"
            )
            hits = found.get("hits", []) if isinstance(found, dict) else []
            threads = [
                str(h.get("objectID"))
                for h in hits
                if isinstance(h, dict)
                and _STORY_TITLE.match(str(h.get("title") or ""))
                and int(h.get("created_at_i") or 0) >= threads_since.timestamp()
                and str(h.get("objectID") or "").isdigit()
            ][:2]
            comments: list[dict[str, Any]] = []
            for story in threads:
                thread = await web.fetch_json(http, self.name, ITEM_URL.format(id=story))
                if isinstance(thread, dict):
                    comments.extend(c for c in thread.get("children") or [] if isinstance(c, dict))
        return self._postings(comments, since)

    def _postings(self, comments: list[dict[str, Any]], since: datetime) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for comment in comments:
            if len(postings) >= MAX_POSTINGS:
                break
            posted = web.parse_time(comment.get("created_at_i"))
            if not str(comment.get("id") or "").isdigit() or posted is None or posted < since:
                continue
            fields = parse_comment(str(comment.get("text") or ""))
            if fields is None:
                continue
            try:
                postings.append(
                    JobPosting.model_validate(
                        {
                            **{k: v for k, v in fields.items() if v},
                            "source": "hackernews",
                            "source_job_id": str(comment["id"]),
                            "url": f"https://news.ycombinator.com/item?id={comment['id']}",
                            "posted_at": posted,
                        }
                    )
                )
            except ValidationError as error:
                log.debug("hackernews: skipped a comment: %d problem(s)", error.error_count())
        return postings
