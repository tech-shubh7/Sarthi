# Fill / approve / submit flow ported from OpenApply (MIT), src/apply/engine.ts @ db7796e.
# Copyright (c) OpenApply contributors. Modifications (c) Sarthi contributors: Python Playwright; the approval
# is a Telegram button carrying the one-time code; the browser is headless and kept open between fill and
# submit for a limited time.
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
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from sarthi.apply import adapters, linkedin, naukri
from sarthi.apply.adapters import Adapter, FillError
from sarthi.apply.agent import ApplicationBrowser, Walked
from sarthi.jobs.forms import FormContext, FormField, suggest

log = logging.getLogger(__name__)

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

    def __init__(
        self,
        shots_dir: Path,
        *,
        profile_dir: Path | None = None,
        headless: bool = True,
        browser: ApplicationBrowser | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Screenshots go to ``shots_dir``; ``profile_dir`` keeps the user's site logins between runs.

        ``browser`` walks the sites no adapter knows (and boards with a saved login); None: they are handed
        over. ``env`` shows a visible browser on another screen (the virtual one of ``sarthi.apply.screen``).
        """
        self.shots_dir = shots_dir
        self.browser = browser
        self.profile_dir = profile_dir
        self.headless = headless
        self.env = env
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._open: dict[str, _Open] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, job_id: str) -> asyncio.Lock:
        return self._locks.setdefault(job_id, asyncio.Lock())

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
                )
            else:
                self._browser = await self._playwright.chromium.launch(headless=self.headless)
                self._context = await self._browser.new_context(
                    locale="en-US", viewport={"width": 1280, "height": 1600}
                )
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
        async with self._lock(job_id):
            await self.close_job(job_id)
            page = await self._new_page()
            if linkedin.is_linkedin(url) or naukri.is_naukri(url):
                return await self._fill_board(job_id, page, url, ctx)
            adapter = adapters.adapter_for(url)
            try:
                await page.goto(
                    adapters.apply_url(url), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                )
                adapter = await adapters.refine_for_page(page, adapter)
            except PlaywrightError as error:
                with contextlib.suppress(PlaywrightError):
                    await page.close()
                raise FillError(f"the form did not load: {str(error).splitlines()[0][:200]}") from error
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

    async def submit(self, job_id: str, code: str) -> SubmitResult:
        """Submit the approved fill. Refuses without the code, a still-open page, or with blanks left."""
        async with self._lock(job_id):
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
            try:
                await page.locator(adapter.submit).first.click()
            except PlaywrightError as error:
                return SubmitResult("failed", f"Could not click submit: {str(error).splitlines()[0][:200]}")
            outcome, detail = await self._outcome(page, adapter, opened.url, before)
            if adapter is naukri.NAUKRI and outcome != "submitted" and await naukri.chatbot_open(page):
                outcome, detail = (
                    "needs_human",
                    "Naukri asked follow-up questions in its chatbot: answer them on Naukri.",
                )
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
                if adapters.THANKS.search(now) and (moved or not adapters.THANKS.search(before)):
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
                ):
                    return "submitted", f"Thank-you message shown at {page.url}"
            if (await adapters.detect_captcha(page)).get("interactive"):
                return "needs_human", "A CAPTCHA appeared after clicking submit."
        return "failed", "No confirmation within 20 seconds; check the form before trying again."


LOGIN_PAGES = {
    "linkedin": "https://www.linkedin.com/login",
    "naukri": "https://www.naukri.com/nlogin/login",
    "indeed": "https://secure.indeed.com/auth",
    "instahyre": "https://www.instahyre.com/login/",
    "wellfound": "https://wellfound.com/login",
    "foundit": "https://www.foundit.in/rio/login",
    "glassdoor": "https://www.glassdoor.com/profile/login_input.htm",
    "shine": "https://www.shine.com/myshine/login/",
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
}
_LOGIN_PATH = re.compile(r"log-?in|sign-?in|auth|sso|account|register", re.IGNORECASE)
_SIGNED_IN = {
    "linkedin": re.compile(r"linkedin\.com/(?:feed|jobs|in|mynetwork)/"),
    "naukri": re.compile(r"naukri\.com/(?:mnjuser|homepage|myapply)"),
}
LOGIN_WAIT_S = 600


async def login(
    site: str, profile_dir: Path, *, wait_s: float = LOGIN_WAIT_S, env: dict[str, str] | None = None
) -> bool:
    """Open a visible browser on the site's login page; the user signs in themselves, the session is kept.

    Sarthi never sees or stores the password: only the browser's own session cookies, in ``profile_dir``.
    Returns True once the site shows a signed-in page, False when the window was closed or time ran out.
    ``env`` puts the window on another screen (the virtual one of ``sarthi.apply.screen``).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            env=_browser_env(env),
            args=["--window-position=0,0", "--window-size=1280,900"] if env else [],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(LOGIN_PAGES[site])
        end = time.monotonic() + wait_s
        signed_in = False
        while time.monotonic() < end and not page.is_closed():
            if await _signed_in(page, site):
                signed_in = True
                break
            await asyncio.sleep(1)
        with contextlib.suppress(PlaywrightError):
            await context.close()
        return signed_in


async def _signed_in(page: Page, site: str) -> bool:
    """True once the site shows a page of a signed-in user.

    LinkedIn and Naukri have known addresses for that; elsewhere: the site's own pages again, away from any
    login or sign-up address, with no password field on screen.
    """
    if site in _SIGNED_IN:
        return _SIGNED_IN[site].search(page.url) is not None
    try:
        host = (urlparse(page.url).hostname or "").lower()
        wanted = (urlparse(LOGIN_PAGES[site]).hostname or "").lower().split(".")[-2]
        if wanted not in host or _LOGIN_PATH.search(urlparse(page.url).path):
            return False
        return not await page.evaluate(
            "() => Array.from(document.querySelectorAll('input[type=password]'))"
            ".some((e) => e.getClientRects().length > 0)"
        )
    except PlaywrightError:
        return False


def _browser_env(env: dict[str, str] | None) -> dict[str, str | float | bool] | None:
    """Playwright's type for a browser's environment (None: the current one)."""
    return {key: value for key, value in env.items()} if env else None  # noqa: C416  (the type needs a new dict)
