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

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from sarthi.apply import adapters
from sarthi.apply.adapters import Adapter, FillError
from sarthi.jobs.forms import FormContext, FormField, suggest

log = logging.getLogger(__name__)

PAGE_IDLE_S = 30 * 60  # a filled form waits this long for Submit; then it must be filled again
SUBMIT_WAIT_S = 20.0
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

    def __init__(self, shots_dir: Path, *, headless: bool = True) -> None:
        """Screenshots go to ``shots_dir``."""
        self.shots_dir = shots_dir
        self.headless = headless
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

    async def fill(self, job_id: str, url: str, ctx: FormContext) -> FillResult:
        """Open and fill the form; never submits. Re-filling voids any earlier approval."""
        async with self._lock(job_id):
            await self.close_job(job_id)
            page = await self._new_page()
            adapter = adapters.adapter_for(url)
            try:
                await page.goto(
                    adapters.apply_url(url), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
                )
                adapter = await adapters.refine_for_page(page, adapter)
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
            try:
                await page.locator(adapter.submit).first.click()
            except PlaywrightError as error:
                return SubmitResult("failed", f"Could not click submit: {str(error).splitlines()[0][:200]}")
            outcome, detail = await self._outcome(page, adapter, opened.url)
            shot = await self._screenshot(page, job_id, outcome)
            if outcome == "submitted":
                await self.close_job(job_id)
            return SubmitResult(outcome, detail, shot)

    async def _outcome(self, page: Page, adapter: Adapter, form_url: str) -> tuple[str, str]:
        end = time.monotonic() + SUBMIT_WAIT_S
        while time.monotonic() < end and not page.is_closed():
            await asyncio.sleep(0.5)
            if await adapters.confirmed(page, adapter):
                return "submitted", f"Confirmation seen at {page.url}"
            if why := await adapters.rejected(page, adapter):
                return "verification_failed", why
            if not adapter.strict_success:
                moved = page.url.split("#")[0] != form_url.split("#", maxsplit=1)[0]
                if moved and adapters.THANKS.search(await adapters.body_text(page)):
                    return "submitted", f"Thank-you page seen at {page.url}"
            if (await adapters.detect_captcha(page)).get("interactive"):
                return "needs_human", "A CAPTCHA appeared after clicking submit."
        return "failed", "No confirmation within 20 seconds; check the form before trying again."
