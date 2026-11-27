"""Filling an application form and submitting it only with the user's approval of that exact fill.

``fill`` opens the form, fills every field it has an answer for (files first: some forms read the resume and
prefill fields), takes a screenshot and returns a one-time code. ``submit`` works only with that code, on the
same open page, at the same address, with no required field left blank; it never solves a CAPTCHA (a challenge
goes to the user) and only reports "submitted" when the hiring system's own success page is seen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Request, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Response as PlaywrightResponse

from sarthi.apply import adapters, linkedin, naukri
from sarthi.apply.adapters import Adapter, FillError, PostingClosedError
from sarthi.apply.agent import SIGN_IN_TO_APPLY, ApplicationBrowser, Walked
from sarthi.jobs.forms import FormContext, FormField, suggest
from sarthi.jobs.net import is_public_url

log = logging.getLogger(__name__)

UrlCheck = Callable[[str], Awaitable[bool]]


INTERRUPTED = "a browser window was opened for you during this fill: it is filled again in a minute"


class EnginePausedError(Exception):
    """The browser profile is lent to a visible window (a login, a form finished by hand): try later."""


CLOSED_TEXT = adapters.CLOSED_TEXT
UNKNOWN = "unknown"  # Submit was pressed, no answer seen: perhaps sent
_GONE = frozenset({404, 410})


def _gone(response: PlaywrightResponse | None, url: str) -> None:
    """Raise ``PostingClosedError`` when the job's own address answers 404 or 410 (the posting is gone)."""
    if response is not None and response.status in _GONE:
        raise PostingClosedError(
            f"the posting is closed ({adapters.host_of(url) or 'the site'} answered {response.status}: "
            "the page no longer exists)"
        )


PAGE_IDLE_S = 30 * 60  # a filled form waits this long for Submit; then it must be filled again
SUBMIT_WAIT_S = 20.0
HAND_WAIT_S = 600.0  # finishing a form by hand (a CAPTCHA, then Submit): ten minutes
NAV_TIMEOUT_MS = 45_000
SETTLE_TIMEOUT_MS = 10_000
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6


def new_code() -> str:
    """A short one-time approval code (no look-alike characters)."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


@dataclass
class FillResult:
    """What the fill did."""

    adapter: str
    filled: list[tuple[str, str]] = field(default_factory=list)  # (label, value shown to the user)
    blanks: list[FormField] = field(default_factory=list)  # required, no answer: the user must answer
    failed: list[tuple[str, str]] = field(
        default_factory=list
    )  # (label, why) required but could not be entered
    captcha: bool = False
    screenshot: Path | None = None
    code: str = ""


@dataclass
class SubmitResult:
    """How a submission ended: submitted, needs_human, failed or verification_failed."""

    outcome: str
    detail: str
    screenshot: Path | None = None


@dataclass
class _Open:
    page: Page
    adapter: Adapter
    code: str = ""
    url: str = ""
    blanks: int = 0
    touched: float = field(default_factory=time.monotonic)


class ApplyEngine:
    """One headless browser; one open page per job between fill and submit."""

    def __init__(  # noqa: PLR0913  (explicit dependencies)
        self,
        shots_dir: Path,
        *,
        profile_dir: Path | None = None,
        headless: bool = True,
        browser: ApplicationBrowser | None = None,
        env: dict[str, str] | None = None,
        url_check: UrlCheck | None = is_public_url,
    ) -> None:
        """Screenshots go to ``shots_dir``; ``profile_dir`` keeps the user's site logins between runs.

        ``browser`` walks the sites no adapter knows (and boards with a saved login); None: they are handed
        over. ``env`` shows a visible browser on another screen (the virtual one of ``sarthi.apply.screen``).
        ``url_check``: every address the browser opens or loads (frames and tabs included; the job link's
        redirects are checked after the fact) must pass it. Job links come from public sources, so by
        default only public internet hosts (no requests into the user's own network).
        None turns the guard off: tests only (forms from files and localhost).
        """
        self.shots_dir = shots_dir
        self.browser = browser
        self.profile_dir = profile_dir
        self.headless = headless
        self.env = env
        self.url_check = url_check
        self._public: dict[str, bool] = {}
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._open: dict[str, _Open] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._busy = 0  # fills and submits under way
        self._submitting = 0  # submits under way (a visible window waits for these, never for fills)
        self._generation = 0  # one more each time a visible window takes the browser: older fills are void
        self._held = 0  # visible windows holding the browser profile
        self._idle = asyncio.Condition()

    def _lock(self, job_id: str) -> asyncio.Lock:
        return self._locks.setdefault(job_id, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def _in_use(self, *, submitting: bool = False) -> AsyncIterator[None]:
        """A fill or submit; ``EnginePausedError`` while a visible window holds the profile."""
        if self._held:
            raise EnginePausedError("a browser window is open for you: the application waits for it")
        self._busy += 1
        self._submitting += submitting
        try:
            yield
        finally:
            self._busy -= 1
            self._submitting -= submitting
            async with self._idle:
                self._idle.notify_all()

    @contextlib.asynccontextmanager
    async def _interruptible(self) -> AsyncIterator[None]:
        """A fill that a visible window may cut short: it ends as ``EnginePausedError`` (filled again later).

        Whatever the cut-off fill raised or returned (a closed page, fields it could not enter) says nothing
        about the form, so it never becomes a hand-over to the user.
        """
        generation = self._generation
        try:
            yield
        except EnginePausedError:
            raise
        except Exception as error:
            if self._generation != generation:
                raise EnginePausedError(INTERRUPTED) from error
            raise
        if self._generation != generation:
            raise EnginePausedError(INTERRUPTED)

    @property
    def busy(self) -> bool:
        """True while a fill or a submit is under way."""
        return self._busy > 0

    @contextlib.asynccontextmanager
    async def paused(self) -> AsyncIterator[None]:
        """Lend the browser profile to a visible window at once: new fills wait, running fills are cut short.

        A fill may take minutes, and the user who clicked "Log in" should not wait for it: it is voided
        (``EnginePausedError``, filled again later), never handed over. A submit under way is waited for,
        since cutting one off could leave an application half sent.
        """
        self._held += 1
        try:
            async with self._idle:
                await self._idle.wait_for(lambda: self._submitting == 0)
            self._generation += 1
            await self.aclose()
            yield
        finally:
            self._held -= 1

    async def _allowed(self, url: str) -> bool:
        """``url_check`` for one address, remembered per scheme and host (a page loads many files)."""
        if self.url_check is None:
            return True
        scheme = url.partition(":")[0].lower()
        if scheme in ("data", "blob", "about"):
            return True  # made inside the page: nothing leaves the browser
        key = f"{scheme}://{adapters.host_of(url)}"
        if key not in self._public:
            checked = "http" + url[2:] if scheme in ("ws", "wss") else url
            self._public[key] = await self.url_check(checked)
        return self._public[key]

    async def _guard(self, route: Route, request: Request) -> None:
        """Every request of the browser (pages, frames, tabs, files): non-public addresses are refused."""
        if await self._allowed(request.url):
            await route.continue_()
            return
        log.warning("blocked a request to a non-public address (%s)", adapters.host_of(request.url))
        await route.abort("blockedbyclient")

    async def _check_redirects(self, response: PlaywrightResponse | None) -> None:
        """Refuse a page reached through a redirect to a non-public address.

        Playwright routes only the first request of a redirect chain, so ``_guard`` cannot stop the hop
        itself; its answer is never used (filled, screenshotted or shown to the user).
        """
        request = response.request if response is not None else None
        while request is not None:
            await self._check_link(request.url)
            request = request.redirected_from

    async def _check_link(self, url: str) -> None:
        """Refuse a job link to a non-public address before the browser opens it."""
        if not await self._allowed(url):
            raise FillError(
                f"its link ({adapters.host_of(url) or url[:80]}) is not a public web address, "
                "so Sarthi did not open it"
            )

    async def _new_page(self) -> Page:
        if self._context is None:
            self._playwright = await async_playwright().start()
            if self.profile_dir is not None:
                # The user's saved site logins (`sarthi login`, /login) live in this profile: without it every
                # board that needs an account would meet its login page.
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self._context = await self._playwright.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    headless=self.headless,
                    locale="en-US",
                    viewport={"width": 1280, "height": 1600} if self.headless else None,
                    env=_browser_env(self.env),
                    args=["--window-position=0,0", "--window-size=1280,900"] if self.env else [],
                    service_workers="block",  # a service worker's requests would skip the address guard
                )
            else:
                self._browser = await self._playwright.chromium.launch(headless=self.headless)
                self._context = await self._browser.new_context(
                    locale="en-US", viewport={"width": 1280, "height": 1600}, service_workers="block"
                )
            if self.url_check is not None:
                await self._context.route("**/*", self._guard)
        page = await self._context.new_page()
        page.set_default_timeout(15_000)
        return page

    async def close_job(self, job_id: str) -> None:
        """Forget the job's open page (closing it)."""
        opened = self._open.pop(job_id, None)
        if opened is not None and not opened.page.is_closed():
            with contextlib.suppress(PlaywrightError):
                await opened.page.close()

    async def close_idle(self) -> None:
        """Close forms nobody approved in time."""
        now = time.monotonic()
        for job_id in [j for j, o in self._open.items() if now - o.touched > PAGE_IDLE_S]:
            await self.close_job(job_id)

    async def aclose(self) -> None:
        """Close every page and the browser."""
        for job_id in list(self._open):
            await self.close_job(job_id)
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(PlaywrightError):
                    await closer.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = self._browser = self._context = None

    def can_submit(self, job_id: str, code: str) -> bool:
        """True when this exact fill is still open and ``code`` is its approval code."""
        opened = self._open.get(job_id)
        return bool(opened and opened.code and secrets.compare_digest(opened.code, code.strip().upper()))

    async def _screenshot(
        self, page: Page, job_id: str, tag: str, adapter: Adapter | None = None
    ) -> Path | None:
        """The form itself when it can be found (readable on a phone), else the whole page."""
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", job_id)[:60]
        path = self.shots_dir / f"{safe}-{tag}-{int(time.time())}.png"
        if adapter is not None:
            with contextlib.suppress(PlaywrightError):
                form = page.locator(adapter.root).first
                if await form.is_visible():
                    await form.screenshot(path=str(path))
                    return path
        try:
            await page.screenshot(path=str(path), full_page=True)
        except PlaywrightError:
            return None
        return path

    @property
    def walks(self) -> bool:
        """True when sites without an adapter are walked by the application browser."""
        return self.browser is not None

    async def fill(self, job_id: str, url: str, ctx: FormContext, *, one_click: bool = False) -> FillResult:
        """Open and fill the form; never submits. Re-filling voids any earlier approval.

        ``one_click``: a job board with the user's login, where its "Apply" may send at once (it is held for
        approval).
        """
        async with self._in_use(), self._lock(job_id), self._interruptible():
            await self.close_job(job_id)
            await self._check_link(url)
            page = await self._new_page()
            if linkedin.is_linkedin(url) or naukri.is_naukri(url):
                return await self._fill_board(job_id, page, url, ctx)
            adapter = adapters.adapter_for(url)
            try:
                response = await page.goto(
                    adapters.apply_url(url), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                )
                await self._check_redirects(response)
                _gone(response, url)
                adapter = await adapters.refine_for_page(page, adapter)
            except PlaywrightError as error:
                with contextlib.suppress(PlaywrightError):
                    await page.close()
                raise FillError(f"the form did not load: {str(error).splitlines()[0][:200]}") from error
            except FillError:
                with contextlib.suppress(PlaywrightError):
                    await page.close()
                raise
            if adapter is adapters.GENERIC and self.browser is not None:
                walked = await self._walk(page, url, ctx, one_click=one_click)
                if not walked.known_form:
                    return await self._walked(job_id, walked)
                page = walked.page
                adapter = await adapters.refine_for_page(page, adapters.adapter_for(walked.known_form))
            return await self._fill_with(job_id, page, adapter, ctx)

    async def _walk(self, page: Page, url: str, ctx: FormContext, *, one_click: bool) -> Walked:
        if self.browser is None:
            raise FillError("no application browser")
        try:
            walked = await self.browser.walk(page, url, ctx, one_click=one_click)
        except FillError:
            await self._close_tabs(page)
            raise
        except PlaywrightError as error:
            await self._close_tabs(page)
            raise FillError(
                f"the page did not work as expected: {str(error).splitlines()[0][:200]}"
            ) from error
        if walked.page is not page:
            with contextlib.suppress(PlaywrightError):
                await page.close()  # the job page; the form opened in its own tab
        return walked

    @staticmethod
    async def _close_tabs(page: Page) -> None:
        for tab in list(page.context.pages):
            with contextlib.suppress(PlaywrightError):
                if tab is page or await tab.opener() is page:
                    await tab.close()

    async def _walked(self, job_id: str, walked: Walked) -> FillResult:
        """The application browser's fill, held open for approval like any other."""
        adapter = walked.adapter()
        result = FillResult(adapter.name, walked.filled, walked.blanks, walked.failed, walked.captcha)
        result.screenshot = await self._screenshot(walked.page, job_id, "filled")
        result.code = new_code()
        self._open[job_id] = _Open(
            walked.page, adapter, result.code, walked.page.url, len(result.blanks) + len(result.failed)
        )
        return result

    async def _fill_with(self, job_id: str, page: Page, adapter: Adapter, ctx: FormContext) -> FillResult:
        """A known hiring system's form (or a plain one), filled by its adapter."""
        try:
            fields = await adapters.discover(page, adapter)
        except PlaywrightError as error:
            with contextlib.suppress(PlaywrightError):
                await page.close()
            raise FillError(f"the form did not load: {str(error).splitlines()[0][:200]}") from error
        result = FillResult(adapter=adapter.name)
        ordered = [f for f in fields if f.type == "file"] + [f for f in fields if f.type != "file"]
        for form_field in ordered:
            await self._fill_one(page, adapter, form_field, ctx, result)
        if not fields and not result.filled:
            # Any hiring system: a closed posting shows its page without the form (Greenhouse, Lever and
            # Ashby wait for a form that never comes and find nothing): never offered for Submit.
            closed = CLOSED_TEXT.search(await adapters.body_text(page))
            with contextlib.suppress(PlaywrightError):
                await page.close()
            if closed:
                raise PostingClosedError(f'the posting is closed (the site says "{closed.group(0)}")')
            raise FillError("no application form on the page (the job may be closed)")
        if adapter is adapters.GENERIC and all(_is_search(f) for f in fields):
            # A job description or a site's search box, not an application form: never offered for Submit.
            with contextlib.suppress(PlaywrightError):
                await page.close()
            raise FillError("no application form was found on the page: apply on the site yourself")
        result.captcha = bool((await adapters.detect_captcha(page)).get("interactive"))
        result.screenshot = await self._screenshot(page, job_id, "filled", adapter)
        result.code = new_code()
        self._open[job_id] = _Open(
            page, adapter, result.code, page.url, len(result.blanks) + len(result.failed)
        )
        return result

    async def _fill_board(self, job_id: str, page: Page, url: str, ctx: FormContext) -> FillResult:
        """LinkedIn Easy Apply or Naukri one-click apply, with the user's saved login."""
        try:
            if linkedin.is_linkedin(url):
                adapter: Adapter = linkedin.LINKEDIN
                await linkedin.open_easy_apply(page, url)
                walked = await linkedin.walk(page, ctx, ctx.resume_file)
                result = FillResult(adapter.name, walked.filled, walked.blanks, walked.failed)
                if not walked.ready and not walked.blanks and not walked.failed:
                    result.failed.append(("Easy Apply", walked.note or "the last step was not reached"))
            else:
                adapter = naukri.NAUKRI
                await naukri.open_job(page, url)
                result = FillResult(adapter.name, [("Apply with", "your Naukri profile")])
        except FillError:
            with contextlib.suppress(PlaywrightError):
                await page.close()
            raise
        except PlaywrightError as error:
            with contextlib.suppress(PlaywrightError):
                await page.close()
            raise FillError(
                f"the page did not work as expected: {str(error).splitlines()[0][:200]}"
            ) from error
        result.captcha = bool((await adapters.detect_captcha(page)).get("interactive"))
        result.screenshot = await self._screenshot(page, job_id, "filled", adapter)
        result.code = new_code()
        self._open[job_id] = _Open(
            page, adapter, result.code, page.url, len(result.blanks) + len(result.failed)
        )
        return result

    async def _fill_one(
        self, page: Page, adapter: Adapter, form_field: FormField, ctx: FormContext, result: FillResult
    ) -> None:
        suggestion = suggest(form_field, ctx)
        if suggestion is None:
            if form_field.required:
                result.blanks.append(form_field)
            return
        try:
            await adapters.fill_field(page, adapter, form_field, suggestion.value)
        except (FillError, PlaywrightError) as error:
            if form_field.required:
                result.failed.append((form_field.label, str(error).splitlines()[0][:200]))
            return
        shown = Path(suggestion.value).name if form_field.type == "file" else suggestion.value
        result.filled.append((form_field.label, shown[:200]))
        if form_field.type == "file":
            await page.wait_for_timeout(800)

    async def submit(
        self, job_id: str, code: str, *, before_click: Callable[[], None] | None = None
    ) -> SubmitResult:
        """Submit the approved fill. Refuses without the code, a still-open page, or with blanks left.

        After the press, no confirmation is ``unknown`` (it may have been sent), never ``failed``.
        """
        async with self._in_use(submitting=True), self._lock(job_id):
            if not self.can_submit(job_id, code):
                return SubmitResult(
                    "expired", "That approval is no longer valid (the form was closed or filled again)."
                )
            opened = self._open[job_id]
            opened.code = ""  # single use, whatever happens next
            page, adapter = opened.page, opened.adapter
            if opened.blanks:
                return SubmitResult("needs_human", "Required fields are still blank on the form.")
            if page.url.split("#")[0] != opened.url.split("#")[0]:
                return SubmitResult("expired", "The form page changed after it was filled.")
            if (await adapters.detect_captcha(page)).get("interactive"):
                return SubmitResult("needs_human", "The form shows a CAPTCHA, which Sarthi never solves.")
            before = await adapters.body_text(page)
            # The application browser's walk names the exact button it stopped at: that one first.
            prefer = str(adapter.extra.get("button") or "")
            if refused := await adapters.press_submit(
                page, adapter.submit, prefer=prefer, before_click=before_click
            ):
                return SubmitResult(*refused, await self._screenshot(page, job_id, refused[0]))
            try:
                outcome, detail = await self._outcome(page, adapter, opened.url, before)
                if adapter is naukri.NAUKRI and outcome != "submitted" and await naukri.chatbot_open(page):
                    outcome, detail = (
                        "needs_human",
                        "Naukri asked follow-up questions in its chatbot: answer them on Naukri.",
                    )
            except PlaywrightError:
                outcome, detail = UNKNOWN, "The form stopped responding after Submit. Check the site."
            shot = await self._screenshot(page, job_id, outcome)
            if outcome == "submitted":
                await self.close_job(job_id)
            return SubmitResult(outcome, detail, shot)

    async def watch(self, job_id: str, wait_s: float = HAND_WAIT_S) -> SubmitResult:
        """The user finishes a filled form themselves (solves its CAPTCHA, presses Submit): wait and tell.

        Submitted when the site confirms or shows a thank-you page; needs_human when the window was closed or
        ``wait_s`` passed first. Sarthi presses nothing here.
        """
        opened = self._open.get(job_id)
        if opened is None:
            return SubmitResult("expired", "The filled form is no longer open.")
        page, adapter, form_url = opened.page, opened.adapter, opened.url
        before = await adapters.body_text(page)
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if page.is_closed():
                return SubmitResult("needs_human", "The window was closed before the form was sent.")
            try:
                if await adapters.confirmed(page, adapter):
                    shot = await self._screenshot(page, job_id, "submitted")
                    return SubmitResult("submitted", f"You sent it; confirmation seen at {page.url}", shot)
                now = await adapters.body_text(page)
                moved = page.url.split("#")[0] != form_url.split("#", maxsplit=1)[0]
                if adapters.THANKS.search(now) and (
                    moved or (not adapters.THANKS.search(before) and await adapters.form_gone(page, adapter))
                ):
                    shot = await self._screenshot(page, job_id, "submitted")
                    return SubmitResult("submitted", f"You sent it; thank-you page seen at {page.url}", shot)
            except PlaywrightError:
                pass  # the page is navigating: look again in a moment
            await asyncio.sleep(1.5)
        return SubmitResult("needs_human", f"Not sent within {round(wait_s / 60)} minutes.")

    async def _outcome(
        self, page: Page, adapter: Adapter, form_url: str, before: str = ""
    ) -> tuple[str, str]:
        end = time.monotonic() + SUBMIT_WAIT_S
        while time.monotonic() < end and not page.is_closed():
            await asyncio.sleep(0.5)
            if await adapters.confirmed(page, adapter):
                return "submitted", f"Confirmation seen at {page.url}"
            if why := await adapters.rejected(page, adapter):
                return "verification_failed", why
            if not adapter.strict_success:
                moved = page.url.split("#")[0] != form_url.split("#", maxsplit=1)[0]
                now = await adapters.body_text(page)
                if moved and adapters.THANKS.search(now):
                    return "submitted", f"Thank-you page seen at {page.url}"
                # Many sites show the thanks in place, at the same address: new thank-you text counts too.
                if (
                    adapter.extra.get("agent")
                    and adapters.THANKS.search(now)
                    and not adapters.THANKS.search(before)
                    and await adapters.form_gone(page, adapter)
                ):
                    return "submitted", f"Thank-you message shown at {page.url}"
            if why := await _held_up(page):
                return "needs_human", why
        # Submit was pressed: it may well have gone out. Never "failed" (a retry could send it twice).
        return UNKNOWN, (
            "Submit was pressed but no confirmation appeared within 20 seconds: it may have been sent. "
            "Check the site (or your email) before applying again."
        )


async def _held_up(page: Page) -> str:
    """Why the press led nowhere the user wanted, or "": a CAPTCHA, or a board's sign-in wall."""
    if (await adapters.detect_captcha(page)).get("interactive"):
        return "A CAPTCHA appeared after clicking submit."
    if SIGN_IN_TO_APPLY.search(await adapters.body_text(page)):
        # A board's Apply led to its guest page ("Let's get you applied ... Sign in to apply"): nothing went
        # out; the user is signed out there (a login fills it again).
        return (
            f"{urlparse(page.url).hostname or 'the site'} shows you as signed out: nothing was sent. "
            "Log in once (Settings or /login) and it is filled again."
        )
    return ""


_SEARCH = re.compile(r"\b(?:search|keywords?|find jobs?)\b", re.IGNORECASE)


def _is_search(form_field: FormField) -> bool:
    """A site's search box (often the only "form" on a careers page), not a question of an application."""
    return bool(_SEARCH.search(form_field.label))


LOGIN_PAGES = {
    "linkedin": "https://www.linkedin.com/login",
    "naukri": "https://www.naukri.com/nlogin/login",
    "indeed": "https://secure.indeed.com/auth",
    "instahyre": "https://www.instahyre.com/login/",
    "wellfound": "https://wellfound.com/login",
    "foundit": "https://www.foundit.in/rio/login",
    "glassdoor": "https://www.glassdoor.com/profile/login_input.htm",
    "shine": "https://www.shine.com/myshine/login/",
    "hirist": "https://www.hirist.tech/login",
}
SITE_NAMES = {
    "linkedin": "LinkedIn",
    "naukri": "Naukri",
    "indeed": "Indeed",
    "instahyre": "Instahyre",
    "wellfound": "Wellfound",
    "foundit": "foundit",
    "glassdoor": "Glassdoor",
    "shine": "Shine",
    "hirist": "Hirist",
}
_LOGIN_PATH = re.compile(r"log-?in|sign-?in|auth|sso|account|register", re.IGNORECASE)
_SIGNED_IN = {
    "linkedin": re.compile(r"^/(?:feed|jobs|in|mynetwork)(?:/|$)"),
    "naukri": re.compile(r"^/(?:mnjuser|homepage|myapply)(?:/|$)"),
}
LOGIN_WAIT_S = 600
# A page only a signed-in user sees; a signed-out visit is sent to the login page instead.
SIGNED_IN_CHECK = {
    "linkedin": "https://www.linkedin.com/feed/",
    "naukri": "https://www.naukri.com/mnjuser/homepage",
    # Account pages that send a signed-out visitor to the login page. The login pages themselves look the
    # same signed in or out, so checking them said "not logged in" to a signed-in Indeed user (2026-11-27).
    "indeed": "https://profile.indeed.com/",
    "shine": "https://www.shine.com/myshine/home/",
    "instahyre": "https://www.instahyre.com/candidate/opportunities/",
    "hirist": "https://www.hirist.tech/my-profile",
    "foundit": "https://www.foundit.in/seeker/dashboard",
}
LOGIN_CHECK_S = 30


async def login(
    site: str, profile_dir: Path, *, wait_s: float = LOGIN_WAIT_S, env: dict[str, str] | None = None
) -> bool:
    """Open the site's login page in a plain browser window; the user signs in and closes it.

    The window is an ordinary browser that Sarthi does not control while the user types (no automation
    session attached): sites such as LinkedIn answer an automated window with CAPTCHA after CAPTCHA. It
    uses Sarthi's own browser profile, so the session stays for applying. Sarthi never sees or stores the
    password, only the browser's session cookies in ``profile_dir``. Returns True when, after the window
    is closed (or ``wait_s`` passed), the site recognises the user. ``env`` puts the window on another
    screen (the virtual one of ``sarthi.apply.screen``).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        executable = playwright.chromium.executable_path
    args = [executable, f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check"]
    if env:
        args += ["--window-position=0,0", "--window-size=1280,900"]
    window = await _open_window([*args, LOGIN_PAGES[site]], {**os.environ, **(env or {})})
    try:
        await asyncio.wait_for(window.wait(), wait_s)
    except TimeoutError:
        window.terminate()
        await window.wait()
    return await signed_in(site, profile_dir)


async def _open_window(args: list[str], env: dict[str, str]) -> asyncio.subprocess.Process:
    """Start a plain browser process (replaced in tests)."""
    return await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )


async def signed_in(site: str, profile_dir: Path) -> bool:
    """True when the saved browser profile is signed in to ``site`` (checked out of sight)."""
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(str(profile_dir), headless=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            target = SIGNED_IN_CHECK.get(site, LOGIN_PAGES[site])
            response = await page.goto(target, wait_until="domcontentloaded", timeout=LOGIN_CHECK_S * 1000)
            if response is None or not response.ok:
                return False
            await page.wait_for_timeout(2000)  # a client-side redirect to the login page
            return await _signed_in(page, site)
        except PlaywrightError:
            return False
        finally:
            with contextlib.suppress(PlaywrightError):
                await context.close()


async def _signed_in(page: Page, site: str) -> bool:
    """True once the site shows a page of a signed-in user.

    LinkedIn and Naukri have known addresses for that; elsewhere: the site's own pages again, away from any
    login or sign-up address, with no password field on screen.
    """
    try:
        url = urlparse(page.url)
        host = (url.hostname or "").lower()
        login_host = urlparse(LOGIN_PAGES[site]).hostname or ""
        # These configured sites all have a single-label public suffix.
        domain = ".".join(login_host.split(".")[-2:])
        if host != domain and not host.endswith("." + domain):
            return False
        if allowed := _SIGNED_IN.get(site):
            return allowed.match(url.path) is not None
        if _LOGIN_PATH.search(url.path):
            return False
        return not await page.evaluate(
            "() => Array.from(document.querySelectorAll('input[type=password]'))"
            ".some((e) => e.getClientRects().length > 0)"
        )
    except (PlaywrightError, ValueError):
        return False


def _browser_env(env: dict[str, str] | None) -> dict[str, str | float | bool] | None:
    """Playwright's type for a browser's environment (None: the current one)."""
    return {key: value for key, value in env.items()} if env else None  # noqa: C416  (the type needs a new dict)
