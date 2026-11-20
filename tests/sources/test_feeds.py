"""Public feeds, Hacker News and Freehire against recorded-shape answers, including hostile ones."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from sarthi.jobs.models import EmploymentType, RemoteType, SalaryPeriod, Seniority
from sarthi.jobs.normalize import complete
from sarthi.queue.tasks import PermanentError, RetryableError
from sarthi.sources import web
from sarthi.sources.base import SearchQuery
from sarthi.sources.feeds import FEEDS, FeedSource
from sarthi.sources.freehire import FreehireSource
from sarthi.sources.hackernews import OPEN_ROLE, HackerNewsSource, parse_comment

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
EPOCH_NOW = int(NOW.timestamp())


def clock() -> datetime:
    return NOW


def transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def answer(body: Any, status: int = 200, **headers: str) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    return transport(handle)


async def read(feed: str, body: Any, hours_old: int | None = None, **kwargs: Any) -> list[Any]:
    source = FeedSource(feed, transport=answer(body, **kwargs), clock=clock)
    return await source.fetch(SearchQuery(hours_old=hours_old))


# ---- each feed's own shape -------------------------------------------------------------------------------


async def test_remotive() -> None:
    jobs = await read(
        "remotive",
        {
            "jobs": [
                {
                    "id": 1,
                    "url": "https://remotive.com/remote-jobs/dev/1",
                    "title": "Backend Engineer",
                    "company_name": "Acme",
                    "job_type": "full_time",
                    "publication_date": "2026-11-19T10:00:00",
                    "candidate_required_location": "USA only",
                    "description": "<p>Build <b>APIs</b></p><script>alert(1)</script>",
                }
            ]
        },
    )
    (job,) = jobs
    assert (job.source, job.title, job.company) == ("remotive", "Backend Engineer", "Acme")
    assert job.remote_type is RemoteType.REMOTE
    assert job.employment_type is EmploymentType.FULL_TIME
    assert job.description == "Build APIs"
    assert job.posted_at == datetime(2026, 11, 19, 10, tzinfo=UTC)
    filled = complete(job)
    assert filled.remote_countries == {"US"}  # "USA only" is not "anywhere"


async def test_remoteok_skips_the_legal_notice_and_reads_salary() -> None:
    jobs = await read(
        "remoteok",
        [
            {"legal": "terms"},
            {
                "id": "7",
                "epoch": str(EPOCH_NOW),
                "position": "Data Engineer",
                "company": "Beta",
                "location": "",
                "url": "https://remoteok.com/remote-jobs/7",
                "salary_min": 90000,
                "salary_max": 120000,
            },
            {"id": "8", "position": "No company", "url": "https://remoteok.com/remote-jobs/8"},
        ],
    )
    (job,) = jobs
    assert job.salary is not None
    assert (job.salary.currency, job.salary.minimum, job.salary.maximum) == ("USD", 90000, 120000)
    assert job.location_text == "Worldwide"
    assert complete(job).is_remote_anywhere


async def test_himalayas_restrictions_and_salary_period() -> None:
    jobs = await read(
        "himalayas",
        {
            "jobs": [
                {
                    "title": "Designer",
                    "companyName": "Gamma",
                    "applicationLink": "https://himalayas.app/companies/gamma/jobs/designer",
                    "locationRestrictions": ["India", "Singapore"],
                    "employmentType": "Part Time",
                    "minSalary": 30,
                    "maxSalary": 40,
                    "currency": "usd",
                    "salaryPeriod": "hourly",
                    "pubDate": EPOCH_NOW,
                }
            ]
        },
    )
    (job,) = jobs
    assert job.employment_type is EmploymentType.PART_TIME
    assert job.salary is not None
    assert job.salary.period is SalaryPeriod.HOUR
    assert complete(job).remote_countries == {"IN", "SG"}


async def test_jobicy_and_workingnomads() -> None:
    (jobicy,) = await read(
        "jobicy",
        {
            "jobs": [
                {
                    "id": 3,
                    "url": "https://jobicy.com/jobs/3",
                    "jobTitle": "QA Analyst",
                    "companyName": "Delta",
                    "jobGeo": "Anywhere",
                    "jobType": ["Contract"],
                    "pubDate": "2026-11-20T08:39:50+00:00",
                }
            ]
        },
    )
    assert jobicy.employment_type is EmploymentType.CONTRACT
    (nomads,) = await read(
        "workingnomads",
        [
            {
                "url": "https://www.workingnomads.com/job/go/1/",
                "title": "Support Lead",
                "company_name": "Eps",
                "location": "Europe",
                "pub_date": "2026-11-19T07:13:33-05:00",
            }
        ],
    )
    assert nomads.posted_at == datetime(2026, 11, 19, 12, 13, 33, tzinfo=UTC)


async def test_arbeitnow_is_not_all_remote() -> None:
    jobs = await read(
        "arbeitnow",
        {
            "data": [
                {
                    "slug": "a",
                    "company_name": "Zeta",
                    "title": "Werkstudent",
                    "remote": False,
                    "url": "https://www.arbeitnow.com/jobs/a",
                    "location": "Munich",
                    "job_types": ["Internship"],
                    "created_at": EPOCH_NOW,
                },
                {
                    "slug": "b",
                    "company_name": "Zeta",
                    "title": "Python Dev",
                    "remote": True,
                    "url": "https://www.arbeitnow.com/jobs/b",
                    "location": "",
                },
            ]
        },
    )
    onsite, remote = jobs
    assert onsite.remote_type is RemoteType.UNKNOWN
    assert onsite.employment_type is EmploymentType.INTERNSHIP
    assert complete(onsite).country_codes == {"DE"}
    assert remote.remote_type is RemoteType.REMOTE


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Fin: Senior Manager, Solutions</title><region>Anywhere in the World</region>
<type>Full-Time</type><link>https://weworkremotely.com/remote-jobs/fin-1</link>
<pubDate>Thu, 19 Nov 2026 10:00:00 +0000</pubDate>
<description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>No company separator</title><link>https://weworkremotely.com/remote-jobs/2</link></item>
</channel></rss>"""


async def test_weworkremotely_rss() -> None:
    jobs = await read("weworkremotely", RSS)
    (job,) = jobs  # the second item has no company, so it is not a usable job
    assert (job.company, job.title) == ("Fin", "Senior Manager, Solutions")
    assert job.description == "Hello world"
    assert job.employment_type is EmploymentType.FULL_TIME


# ---- hostile and broken answers ----------------------------------------------------------------------------


async def test_xml_bombs_and_broken_xml_are_refused() -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
        "<rss><channel><item><title>&lol2;</title></item></channel></rss>"
    )
    with pytest.raises(Exception, match=r"(?i)entit|forbidden"):
        await read("weworkremotely", bomb)
    with pytest.raises(RetryableError):
        await read("weworkremotely", "<rss><channel><item>")


@pytest.mark.parametrize("feed", sorted(FEEDS))
@pytest.mark.parametrize(
    "body",
    [{"unexpected": True}, "not json at all", [], {"jobs": "x", "data": None}, [1, "a", None]],
)
async def test_odd_answers_never_crash(feed: str, body: Any) -> None:
    try:
        jobs = await read(feed, body)
    except RetryableError:
        return
    assert jobs == []


@pytest.mark.parametrize("feed", sorted(FEEDS))
async def test_rows_with_junk_values_are_skipped_not_fatal(feed: str) -> None:
    junk = {
        "id": {"x": 1},
        "url": "javascript:alert(1)",
        "title": None,
        "company_name": ["a"],
        "pubDate": "yesterday",
        "minSalary": "lots",
        "currency": "dollars",
    }
    body: Any = {"jobs": [junk], "data": [junk]} if feed not in ("remoteok", "workingnomads") else [junk]
    if feed == "weworkremotely":
        body = "<rss><channel><item><title>A: B</title><link>ftp://x</link></item></channel></rss>"
    assert await read(feed, body) == []


@pytest.mark.parametrize(
    ("status", "error"), [(429, RetryableError), (503, RetryableError), (404, PermanentError)]
)
async def test_http_errors_map_to_queue_errors(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        await read("remotive", {}, status=status)


async def test_retry_after_is_kept() -> None:
    with pytest.raises(RetryableError) as caught:
        await read("remotive", {}, status=429, **{"retry-after": "120"})
    assert caught.value.retry_after == 120


async def test_network_failure_is_retryable() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(RetryableError):
        await FeedSource("remotive", transport=transport(fail), clock=clock).fetch(SearchQuery())


async def test_huge_answer_is_refused() -> None:
    with pytest.raises(RetryableError, match="too large"):
        await read("remotive", "x" * (web.MAX_BYTES + 1))


async def test_hours_old_keeps_recent_and_undated_jobs() -> None:
    def job(i: int, when: Any) -> dict[str, Any]:
        return {
            "id": i,
            "url": f"https://r.io/{i}",
            "title": "Dev",
            "company_name": "Co",
            "publication_date": when,
        }

    body = {"jobs": [job(1, "2026-11-20T06:00:00"), job(2, "2026-11-01T00:00:00"), job(3, None)]}
    jobs = await read("remotive", body, hours_old=24)
    assert sorted(j.source_job_id for j in jobs) == ["1", "3"]


def test_unknown_feed_is_rejected() -> None:
    with pytest.raises(KeyError):
        FeedSource("myspace")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<p>a</p><p>b</p>", "a\nb"),
        ("&lt;script&gt;alert(1)&lt;/script&gt;ok", "ok"),
        ("&lt;b&gt;bold&lt;/b&gt;", "bold"),
        ("x\u202eevil\u200b", "xevil"),
        ("a\x00b\x07c", "abc"),
        ("  lots   of space ", "lots of space"),
        ("", ""),
        (None, ""),
    ],
)
def test_plain_text(raw: str | None, expected: str) -> None:
    assert web.plain_text(raw) == expected


def test_plain_text_cuts_long_input() -> None:
    assert len(web.plain_text("word " * 100_000)) <= web.TEXT_CHARS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (EPOCH_NOW, NOW),
        (str(EPOCH_NOW), NOW),
        ("2026-11-20T12:00:00Z", NOW),
        ("2026-11-20T12:00:00", NOW),
        ("Fri, 20 Nov 2026 12:00:00 +0000", NOW),
        ("yesterday", None),
        ("", None),
        (None, None),
        (True, None),
        (10**20, None),
    ],
)
def test_parse_time(value: Any, expected: datetime | None) -> None:
    assert web.parse_time(value) == expected


# ---- Hacker News -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("comment", "company", "title", "place"),
    [
        (
            "Acme | Senior Backend Engineer | Berlin | REMOTE | Full-time<p>We build things.",
            "Acme",
            "Senior Backend Engineer",
            "Berlin, REMOTE",
        ),
        ("acme.io | Founding Engineer | Onsite SF", "Acme", "Founding Engineer", "Onsite SF"),
        ("Gamma | $150k | equity | Toronto", "Gamma", OPEN_ROLE, "Toronto"),
    ],
)
def test_hn_comment_header(comment: str, company: str, title: str, place: str) -> None:
    fields = parse_comment(comment)
    assert fields is not None
    assert (fields["company"], fields["title"], fields["location_text"]) == (company, title, place)


@pytest.mark.parametrize("comment", ["", "Thanks for posting!", "<script>alert(1)</script>", "   "])
def test_hn_non_job_comments_are_ignored(comment: str) -> None:
    assert parse_comment(comment) is None


def test_hn_apply_link_only_when_it_looks_like_a_jobs_page() -> None:
    fields = parse_comment(
        'Acme | Engineer | Remote<p><a href="https://acme.io/careers/1">apply</a> '
        '<a href="javascript:alert(1)">x</a>'
    )
    assert fields is not None
    assert fields["apply_url"] == "https://acme.io/careers/1"
    fields = parse_comment('Acme | Engineer<p><a href="https://acme.io/blog">blog</a>')
    assert fields is not None
    assert fields["apply_url"] is None


def hn_transport(
    children: list[dict[str, Any]], title: str = "Ask HN: Who is hiring? (November 2026)"
) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if "search_by_date" in request.url.path:
            hits = [
                {"objectID": "100", "title": title, "created_at_i": EPOCH_NOW - 86400 * 10},
                {"objectID": "99", "title": "Ask HN: Who wants to be hired?", "created_at_i": EPOCH_NOW},
                {"objectID": "abc", "title": title, "created_at_i": EPOCH_NOW},
            ]
            return httpx.Response(200, json={"hits": hits})
        assert request.url.path.endswith("/items/100")
        return httpx.Response(200, json={"children": children})

    return transport(handle)


async def test_hn_reads_recent_job_comments() -> None:
    children = [
        {"id": 1, "created_at_i": EPOCH_NOW - 3600, "text": "Acme | Backend Engineer | Remote (EU)"},
        {"id": 2, "created_at_i": EPOCH_NOW - 3600, "text": "Great thread!"},
        {"id": "x", "created_at_i": EPOCH_NOW, "text": "Beta | Engineer"},
        {"id": 3, "created_at_i": EPOCH_NOW - 86400 * 90, "text": "Old | Engineer"},
        {"id": 4, "created_at_i": EPOCH_NOW - 86400 * 3, "text": "Gamma | Designer | NYC"},
        "not a dict",
    ]
    source = HackerNewsSource(transport=hn_transport(children), clock=clock)  # type: ignore[arg-type]
    jobs = await source.fetch(SearchQuery())
    assert [j.company for j in jobs] == ["Acme", "Gamma"]
    assert str(jobs[0].url) == "https://news.ycombinator.com/item?id=1"
    recent = await HackerNewsSource(transport=hn_transport(children), clock=clock).fetch(  # type: ignore[arg-type]
        SearchQuery(hours_old=24)
    )
    assert [j.company for j in recent] == ["Acme"]


async def test_hn_without_threads_is_empty() -> None:
    source = HackerNewsSource(transport=hn_transport([], title="Ask HN: something else"), clock=clock)
    assert await source.fetch(SearchQuery()) == []


# ---- Freehire ----------------------------------------------------------------------------------------------


def freehire_job(i: int, **extra: Any) -> dict[str, Any]:
    return {
        "public_slug": f"job-{i}",
        "url": f"https://boards.example.com/job/{i}",
        "title": "Data Engineer",
        "company": "Acme",
        "location": "Hyderabad, India",
        "countries": ["in"],
        "work_mode": "remote",
        "enrichment": {"seniority": "senior"},
        "posted_at": "2026-11-20T08:00:00Z",
        "closed_at": None,
        "reality": {"fake_freshness": False},
        **extra,
    }


def freehire(pages: dict[str, list[dict[str, Any]]], meta: dict[str, Any] | None = None) -> Any:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        data = pages.get(request.url.params["q"], [])
        return httpx.Response(200, json={"data": data, "meta": meta or {"total": len(data)}})

    return transport(handle), seen


async def test_freehire_searches_each_role_with_the_users_filters() -> None:
    mock, seen = freehire(
        {
            "Data Engineer": [freehire_job(1), freehire_job(2, closed_at="2026-11-19")],
            "Analytics Engineer": [freehire_job(1), freehire_job(3, reality={"fake_freshness": True})],
        }
    )
    query = SearchQuery(
        roles=("Data Engineer", "Analytics Engineer"), countries=("IN",), remote=True, hours_old=72
    )
    jobs = await FreehireSource(transport=mock).fetch(query)
    assert [j.source_job_id for j in jobs] == ["job-1"]  # duplicate, closed and fake-fresh dropped
    job = jobs[0]
    assert job.remote_type is RemoteType.REMOTE
    assert job.remote_countries == {"IN"}
    assert job.seniority is Seniority.SENIOR
    params = seen[0].url.params
    assert (params["countries"], params["work_mode"], params["open_within_days"]) == ("in", "remote", "3")


async def test_freehire_ignored_filter_fails_the_read() -> None:
    mock, _ = freehire({"Dev": [freehire_job(1)]}, meta={"total": 1, "ignored_params": ["countries"]})
    with pytest.raises(RetryableError, match="ignored"):
        await FreehireSource(transport=mock).fetch(SearchQuery(roles=("Dev",), countries=("IN",)))


async def test_freehire_pages_until_short_page() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        calls.append(str(offset))
        size = 100 if offset < 200 else 5
        data = [freehire_job(offset + i) for i in range(size)]
        return httpx.Response(200, json={"data": data, "meta": {"total": 10_000}})

    jobs = await FreehireSource(transport=transport(handle)).fetch(SearchQuery(roles=("Dev",)))
    assert calls == ["0", "100", "200"]
    assert len(jobs) == 205


@pytest.mark.parametrize("body", [{"data": None}, [], "oops", {"data": [None, 1, {"no": "url"}]}])
async def test_freehire_odd_answers(body: Any) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(body) if not isinstance(body, str) else body)

    try:
        jobs = await FreehireSource(transport=transport(handle)).fetch(SearchQuery(roles=("Dev",)))
    except RetryableError:
        return
    assert jobs == []


async def test_freehire_without_roles_asks_nothing() -> None:
    mock, seen = freehire({})
    assert await FreehireSource(transport=mock).fetch(SearchQuery()) == []
    assert seen == []


def test_freehire_window_is_clamped() -> None:
    long = FreehireSource._params("x", SearchQuery(hours_old=24 * 365), 0)
    short = FreehireSource._params("x", SearchQuery(hours_old=1), 0)
    assert (long["open_within_days"], short["open_within_days"]) == ("30", "1")
    assert "countries" not in FreehireSource._params("x", SearchQuery(), 0)
