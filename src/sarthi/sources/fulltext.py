"""Download a job's own page and keep its main text, for jobs that arrived with only a title or a snippet.

Polite and safe by default: robots.txt is obeyed, each site gets at most one request per second, every
redirect hop must resolve to a public IP (no requests into your own network), and login walls are detected.
The schema.org JobPosting in the page wins over the visible text (it holds the full description even on
JavaScript-only pages such as Workday). When a page cannot be used, the job is matched on what it already has.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura

from sarthi import __version__
from sarthi.queue.tasks import RetryableError
from sarthi.sources.net import is_public_host
from sarthi.sources.structured import description_text, find_job_posting

log = logging.getLogger(__name__)

USER_AGENT = f"Mozilla/5.0 (X11; Linux x86_64) Sarthi/{__version__} (self-hosted job search)"
MIN_TEXT = 400  # shorter than this usually means a JavaScript shell
MAX_HOPS = 5
MAX_BYTES = 5_000_000
TIMEOUT_S = 15.0
_TOO_MANY_REQUESTS = 429
_SERVER_ERROR = 500
_CLIENT_ERROR = 400
_OK = 200

# Where sites send you when a page needs an account.
_LOGIN_PATHS = (
    "linkedin.com/login", "linkedin.com/authwall", "linkedin.com/checkpoint",
    "accounts.google.com", "login.microsoftonline.com", "naukri.com/nlogin",
    "indeed.com/account/login", "glassdoor.co.in/profile/login", "glassdoor.com/profile/login",
)  # fmt: skip
_PASSWORD_INPUT = re.compile(r"<input[^>]+type=[\"']?password", re.IGNORECASE)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Page:
    """What a job page gave: its text, or why it could not be used (``note``)."""

    final_url: str
    title: str | None
    text: str | None  # None when unusable; ``note`` says why
    note: str | None = None
    structured: dict[str, Any] | None = None  # schema.org JobPosting in the page, if any


def _host_path(url: str) -> str:
    parts = urlsplit(url)
    return ((parts.hostname or "").removeprefix("www.") + parts.path).lower()


class Fetcher:
    """Fetches job pages politely, one site at a time."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        per_site_interval: float = 1.0,
        *,
        render_js: bool = False,
    ) -> None:
        """Use ``client`` (redirects followed here, hop by hop); ``render_js`` uses Chromium for JS pages."""
        self._client = client or httpx.AsyncClient(
            timeout=TIMEOUT_S, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )
        self._interval = per_site_interval
        self._next_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._render_js = render_js
        self._browser: Any = None
        self._playwright: Any = None

    async def aclose(self) -> None:
        """Close the connection pool and the browser, if one was started."""
        await self._client.aclose()
        if self._browser is not None:
            await self._browser.close()
            await self._playwright.stop()

    async def _polite(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._next_at.get(host, 0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at[host] = time.monotonic() + self._interval

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: RobotFileParser | None = None
            try:
                await self._polite(parts.hostname or "")
                resp = await self._client.get(origin + "/robots.txt")
                if resp.status_code == _OK:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
            except httpx.HTTPError:
                parser = None  # no robots.txt reachable: allowed
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    async def fetch(self, url: str) -> Page:
        """Fetch and extract. Raises ``RetryableError`` for temporary failures only."""
        current = url
        for _ in range(MAX_HOPS + 1):
            if urlsplit(current).scheme not in ("http", "https"):
                return Page(current, None, None, "not a web link")
            host = urlsplit(current).hostname or ""
            if not await is_public_host(host):
                return Page(current, None, None, "not a public address")
            if not await self._allowed(current):
                return Page(current, None, None, "blocked by robots.txt")
            await self._polite(host)
            try:
                resp = await self._client.get(current)
            except httpx.TimeoutException as e:
                raise RetryableError(f"timeout fetching {host}") from e
            except httpx.TransportError as e:
                raise RetryableError(f"could not reach {host}: {type(e).__name__}") from e
            location = resp.headers.get("location")
            if resp.is_redirect and location:
                current = urljoin(current, location)
                continue
            return await self._read(current, resp)
        return Page(current, None, None, "too many redirects")

    async def _read(self, url: str, resp: httpx.Response) -> Page:
        if resp.status_code == _TOO_MANY_REQUESTS or resp.status_code >= _SERVER_ERROR:
            retry_after = resp.headers.get("Retry-After", "")
            raise RetryableError(
                f"{urlsplit(url).hostname} answered {resp.status_code}",
                retry_after=float(retry_after) if retry_after.isdigit() else None,
                status=resp.status_code,
            )
        if resp.status_code >= _CLIENT_ERROR:
            return Page(url, None, None, f"HTTP {resp.status_code}")
        if "html" not in resp.headers.get("content-type", "html"):
            return Page(url, None, None, "not an HTML page")
        if len(resp.content) > MAX_BYTES:
            return Page(url, None, None, "page too large")
        if _host_path(url).startswith(_LOGIN_PATHS):
            return Page(url, None, None, "login required")
        html = resp.text
        if _PASSWORD_INPUT.search(html) and len(html) < 200_000:  # noqa: PLR2004  (a login page, not a job page)
            return Page(url, None, None, "login required")
        title_match = _TITLE.search(html)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip()[:200] if title_match else None
        text = self._main_text(html, url)
        structured = find_job_posting(html)
        if (text is None or len(text) < MIN_TEXT) and self._render_js:
            rendered_html = await self._render(url)
            rendered = self._main_text(rendered_html, url) if rendered_html else None
            if rendered and len(rendered) > len(text or ""):
                text = rendered
            if rendered_html and structured is None:
                structured = find_job_posting(rendered_html)
        about = description_text(structured) if structured else None
        if about and len(about) > len(text or ""):
            # JavaScript-only job pages (Workday, ...) still carry the full description in their
            # JobPosting data: read that rather than nothing.
            text = about
        return Page(url, title, text or None, None if text else "no readable text", structured)

    @staticmethod
    def _main_text(html: str, url: str) -> str | None:
        text: str | None = trafilatura.extract(
            html, url=url, include_comments=False, include_tables=True, favor_recall=True
        )
        return text.strip() if text else None

    async def _render(self, url: str) -> str | None:
        """The page's HTML after rendering in headless Chromium; None when that fails."""
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415  (loaded only when asked)

            if self._browser is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch()
            page = await self._browser.new_page(user_agent=USER_AGENT)

            async def guard(route: Any) -> None:
                # The page's own scripts and redirects get the same public-only rule.
                if await is_public_host(urlsplit(route.request.url).hostname or ""):
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", guard)
            try:
                await page.goto(url, wait_until="networkidle", timeout=20_000)
                html = await page.content()
            finally:
                await page.close()
        except Exception as e:  # noqa: BLE001  (rendering is best effort)
            log.info("javascript rendering failed for %s: %s", url, str(e)[:200])
            return None
        return str(html)
