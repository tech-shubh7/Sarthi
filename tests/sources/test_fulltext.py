# Fetcher tests ported from JobRadar (MIT), tests/test_fetch.py @ fdd739b, on httpx.MockTransport.
import sqlite3
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from sarthi.db.jobs import JobStore
from sarthi.db.tasks import TaskRecord
from sarthi.jobs import net
from sarthi.queue import names
from sarthi.queue.tasks import MAX_ATTEMPTS, RetryableError, TaskQueue
from sarthi.sources import fulltext
from sarthi.sources.fill import TextFiller
from sarthi.sources.fulltext import Fetcher
from sarthi.sources.structured import description_text, find_job_posting
from tests.matching.conftest import make_job

ARTICLE = (
    "<html><head><title>SDE Intern - Acme Careers</title></head><body><main><h1>SDE Intern</h1>"
    + (
        "<p>Acme is hiring software engineering interns in Bengaluru. "
        "Required skills: Python, SQL. "
        "Eligible batches: 2025 and 2026. Stipend 30,000 per month. Apply before 30 October.</p>" * 6
    )
    + "</main></body></html>"
)
Routes = dict[str, httpx.Response | Exception]


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def check(host: str) -> bool:
        return not host.startswith("10.")

    monkeypatch.setattr(fulltext, "is_public_host", check)


def fetcher(routes: Routes, seen: list[str] | None = None) -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        answer = routes.get(url, httpx.Response(404))
        if isinstance(answer, Exception):
            raise answer
        return answer

    return Fetcher(httpx.AsyncClient(transport=httpx.MockTransport(handler)), per_site_interval=0)


def html(body: str, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html", **headers})


async def test_reads_main_text_and_title() -> None:
    page = await fetcher(
        {
            "https://acme.com/j/1": httpx.Response(301, headers={"Location": "/careers/1"}),
            "https://acme.com/careers/1": html(ARTICLE),
        }
    ).fetch("https://acme.com/j/1")
    assert page.final_url == "https://acme.com/careers/1"
    assert page.title == "SDE Intern - Acme Careers"
    assert page.text
    assert "Python, SQL" in page.text


@pytest.mark.parametrize(
    ("response", "note"),
    [
        (httpx.Response(404), "HTTP 404"),
        (httpx.Response(200, text="%PDF", headers={"content-type": "application/pdf"}), "not an HTML page"),
        (html("<input type='password' name='p'>"), "login required"),
        (html("x" * (fulltext.MAX_BYTES + 1)), "page too large"),
    ],
)
async def test_unusable_pages(response: httpx.Response, note: str) -> None:
    page = await fetcher({"https://acme.com/j": response}).fetch("https://acme.com/j")
    assert page.text is None
    assert page.note == note


async def test_login_redirect_robots_and_private_targets() -> None:
    linkedin = {
        "https://linkedin.com/jobs/view/1": httpx.Response(302, headers={"Location": "/authwall?x=1"}),
        "https://linkedin.com/authwall?x=1": html("<p>Sign in</p>"),
    }
    assert (await fetcher(linkedin).fetch("https://linkedin.com/jobs/view/1")).note == "login required"

    robots = {"https://blocked.com/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /jobs")}
    assert (await fetcher(robots).fetch("https://blocked.com/jobs/9")).note == "blocked by robots.txt"

    seen: list[str] = []
    evil = {"https://evil.com/j": httpx.Response(302, headers={"Location": "http://10.0.0.1/admin"})}
    page = await fetcher(evil, seen).fetch("https://evil.com/j")
    assert page.note == "not a public address"
    assert not any("10.0.0.1" in url for url in seen)  # never contacted


async def test_only_web_links_are_fetched() -> None:
    redirect = {"https://acme.com/j": httpx.Response(302, headers={"Location": "file:///etc/passwd"})}
    assert (await fetcher(redirect).fetch("https://acme.com/j")).note == "not a web link"
    loop = {
        f"https://acme.com/{i}": httpx.Response(302, headers={"Location": f"/{i + 1}"}) for i in range(10)
    }
    assert (await fetcher(loop).fetch("https://acme.com/0")).note == "too many redirects"


async def test_temporary_failures_are_retryable() -> None:
    f = fetcher(
        {
            "https://acme.com/busy": httpx.Response(503),
            "https://acme.com/slow": httpx.ReadTimeout("slow"),
            "https://acme.com/down": httpx.ConnectError("refused"),
            "https://acme.com/limit": httpx.Response(429, headers={"Retry-After": "30"}),
        }
    )
    for url in ("https://acme.com/busy", "https://acme.com/slow", "https://acme.com/down"):
        with pytest.raises(RetryableError):
            await f.fetch(url)
    with pytest.raises(RetryableError) as e:
        await f.fetch("https://acme.com/limit")
    assert e.value.retry_after == 30


async def test_keeps_job_posting_data() -> None:
    ld = '<script type="application/ld+json">{"@type": "JobPosting", "title": "SDE"}</script>'
    page = await fetcher({"https://acme.com/j/2": html(ARTICLE.replace("</head>", ld + "</head>"))}).fetch(
        "https://acme.com/j/2"
    )
    assert page.structured == {"@type": "JobPosting", "title": "SDE"}


async def test_javascript_page_reads_its_jobposting_description() -> None:
    # Workday job pages are JavaScript; their JobPosting data still has the description.
    data = (
        '{"@context": "https://schema.org", "@type": "JobPosting", "title": "Developer", '
        '"description": "Minimum 5 year(s) of experience is required. Java and Spring."}'
    )
    page_html = f"<html><head><script type='application/ld+json'>{data}</script></head><body><div id='root'></div></body></html>"
    page = await fetcher({"https://acme.com/job/1": html(page_html)}).fetch("https://acme.com/job/1")
    assert page.text == "Minimum 5 year(s) of experience is required. Java and Spring."
    assert page.note is None


# ---- structured data ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "block",
    [
        '{"@type": ["JobPosting", "Thing"], "title": "A"}',
        '{"@graph": [{"@type": "Organization"}, {"@type": "JobPosting", "title": "A"}]}',
        '[{"@type": "WebPage"}, {"@type": "JobPosting", "title": "A"}]',
        "{&quot;@type&quot;: &quot;JobPosting&quot;, &quot;title&quot;: &quot;A&quot;}",  # escaped block
    ],
)
def test_job_posting_is_found_in_every_shape(block: str) -> None:
    found = find_job_posting(f"<script type='application/ld+json'>{block}</script>")
    assert found is not None
    assert found["title"] == "A"


def test_broken_or_missing_job_posting() -> None:
    assert find_job_posting("<script type='application/ld+json'>{broken</script>") is None
    assert find_job_posting("<p>no data</p>") is None
    assert description_text({"description": "  "}) is None
    assert description_text({"description": 5}) is None


def test_description_html_becomes_lines() -> None:
    text = description_text(
        {"description": "<p>Build APIs</p><ul><li>Python &amp; SQL</li><li>3 years</li></ul>"}
    )
    assert text == "Build APIs\nPython & SQL\n3 years"


def test_long_descriptions_are_trimmed() -> None:
    block = '{"@type": "JobPosting", "description": "' + "x" * 30_000 + '"}'
    found = find_job_posting(f"<script type='application/ld+json'>{block}</script>")
    assert found is not None
    assert len(found["description"]) == 20_000


# ---- the public-address check ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "addresses", "public"),
    [
        ("localhost", None, False),
        ("printer.local", None, False),
        ("db.internal", None, False),
        ("127.0.0.1", None, False),
        ("[::1]", None, False),
        ("169.254.169.254", None, False),  # cloud metadata
        ("93.184.216.34", None, True),
        ("jobs.example", ["93.184.216.34"], True),
        ("sneaky.example", ["93.184.216.34", "192.168.1.5"], False),  # one private address is enough
        ("nowhere.example", OSError("no such name"), False),
        ("", None, False),
    ],
)
async def test_is_public_host(
    monkeypatch: pytest.MonkeyPatch, host: str, addresses: list[str] | Exception | None, public: bool
) -> None:
    async def resolve(name: str) -> set[str]:
        if isinstance(addresses, Exception):
            raise addresses
        return set(addresses or [])

    monkeypatch.setattr(net, "resolve", resolve)
    assert await net.is_public_host(host) is public


# ---- the fill_text task ------------------------------------------------------------------------------------


class FakeFetcher:
    def __init__(self, result: fulltext.Page | Exception) -> None:
        self.result = result
        self.closed = False

    async def fetch(self, url: str) -> fulltext.Page:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def filled(job_store: JobStore, queue: TaskQueue) -> Callable[[Any], tuple[TextFiller, str]]:
    def make(result: fulltext.Page | Exception) -> tuple[TextFiller, str]:
        saved = job_store.save(make_job(description="Snippet only."))
        return TextFiller(job_store, queue, FakeFetcher(result)), saved.job_id  # type: ignore[arg-type]

    return make


async def test_the_page_text_is_kept_and_the_job_matched(
    filled: Any, job_store: JobStore, queue: TaskQueue
) -> None:
    filler, job_id = filled(fulltext.Page("u", None, "Requires 3+ years of Python. " * 20))
    await filler.handle(TaskRecord(1, names.FILL_TEXT, job_id, {}, 1))
    assert job_store.full_text(job_id).startswith("Requires 3+ years")
    match = queue.claim([names.MATCH])
    assert match is not None
    assert match.key == job_id


async def test_an_unusable_page_still_gets_the_job_matched(
    filled: Any, job_store: JobStore, queue: TaskQueue
) -> None:
    filler, job_id = filled(fulltext.Page("u", None, None, "blocked by robots.txt"))
    await filler.handle(TaskRecord(1, names.FILL_TEXT, job_id, {}, 1))
    assert job_store.full_text(job_id) == "Snippet only."
    assert queue.claim([names.MATCH]) is not None


async def test_a_shorter_page_text_never_replaces_a_longer_one(filled: Any, job_store: JobStore) -> None:
    filler, job_id = filled(fulltext.Page("u", None, "Hi"))
    await filler.handle(TaskRecord(1, names.FILL_TEXT, job_id, {}, 1))
    assert job_store.full_text(job_id) == "Snippet only."


async def test_a_failing_site_is_retried_then_given_up(filled: Any, queue: TaskQueue) -> None:
    filler, job_id = filled(RetryableError("site down"))
    with pytest.raises(RetryableError):
        await filler.handle(TaskRecord(1, names.FILL_TEXT, job_id, {}, 1))
    assert queue.claim([names.MATCH]) is None
    await filler.handle(TaskRecord(1, names.FILL_TEXT, job_id, {}, MAX_ATTEMPTS))  # last try: match anyway
    assert queue.claim([names.MATCH]) is not None


async def test_a_job_gone_meanwhile_is_skipped(job_store: JobStore, queue: TaskQueue) -> None:
    fake = FakeFetcher(RetryableError("not called"))
    filler = TextFiller(job_store, queue, fake)  # type: ignore[arg-type]
    await filler.handle(TaskRecord(1, names.FILL_TEXT, "missing", {}, 1))
    assert queue.claim([names.MATCH]) is None
    await filler.aclose()
    assert fake.closed


def test_set_full_text_replaces_and_keeps_the_limit(job_store: JobStore, conn: sqlite3.Connection) -> None:
    job_id = job_store.save(make_job(description="a")).job_id
    job_store.set_full_text(job_id, "b" * 20_000)
    assert job_store.full_text(job_id) == "b" * 12_000
    job_store.set_full_text(job_id, "")  # nothing to keep: the old text stays
    assert job_store.full_text(job_id) == "b" * 12_000
    other = job_store.save(make_job(url="https://boards.greenhouse.io/x/jobs/9", description="")).job_id
    job_store.set_full_text(other, "first text for a job saved without any")
    assert job_store.full_text(other) == "first text for a job saved without any"
