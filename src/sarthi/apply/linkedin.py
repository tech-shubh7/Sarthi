"""LinkedIn Easy Apply with the user's own logged-in session.

Steps: open the job; stop if LinkedIn says the application was already submitted or the daily Easy Apply
limit is reached; click Easy Apply (a job with only the plain "Apply" applies on the company's own site: its
address is read and raised as ``ExternalApplyError``, and the application continues there); on each step fill
what the profile answers, then Next / Review, until the "Submit application" step, which is left for the
user's approval. A required question without an answer, a validation error that stays, or a login page stops
the flow.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from sarthi.apply import adapters, external
from sarthi.apply.adapters import Adapter, FillError
from sarthi.jobs.forms import FormContext, FormField, suggest

MAX_STEPS = 12
STEP_PAUSE_S = 1.2
MODAL = '[data-testid="dialog-content"], .jobs-easy-apply-modal, div[role="dialog"]'
LINKEDIN = Adapter(
    name="linkedin",
    root=MODAL,
    container="fieldset, .fb-dash-form-element, .jobs-easy-apply-form-section__grouping, "
    ".jobs-easy-apply-form-element",
    label="legend, label",
    submit='button:has-text("Submit application")',
    success_text=re.compile(r"your application was sent|application sent|application submitted", re.I),
    strict_success=True,
)
_LOGIN = re.compile(r"/(?:login|uas/login|signup|authwall|checkpoint)", re.I)
_LIMIT = re.compile(
    r"reached today.{0,40}easy apply limit|daily submissions|continue applying tomorrow", re.I
)
_ALREADY = re.compile(r"application submitted|you applied|applied \d+ \w+ ago", re.I)
# The plain "Apply" of a job without Easy Apply: it leads to the company's own site.
COMPANY_SITE = (
    '.jobs-apply-button, button[aria-label*="company website" i], a[aria-label*="company website" i], '
    'button:text-is("Apply"), a:text-is("Apply"), button:text-is("Apply now"), a:text-is("Apply now")'
)
_NEXT_WORDS = ("submit application", "review", "next", "continue to next step", "continue")
_ERRORS = ", ".join(
    (
        '[data-testid="text-input-helper-text"] > p:not([aria-live])',
        "fieldset + p",
        ".artdeco-inline-feedback--error",
    )
)


def is_linkedin(url: str) -> bool:
    """A LinkedIn job page."""
    return adapters.host_of(url).endswith("linkedin.com")


class LoginNeededError(FillError):
    """The saved session is missing or expired: the user must log in once."""


class LimitReachedError(FillError):
    """LinkedIn's own daily Easy Apply limit is reached."""


@dataclass
class StepResult:
    """What the Easy Apply walk ended with."""

    filled: list[tuple[str, str]] = field(default_factory=list)
    blanks: list[FormField] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    ready: bool = False  # the "Submit application" step is on screen
    note: str = ""


async def _text(page: Page) -> str:
    return await adapters.body_text(page)


async def _modal(page: Page) -> Locator | None:
    modal = page.locator(MODAL).first
    try:
        await modal.wait_for(state="visible", timeout=10_000)
    except PlaywrightError:
        return None
    return modal


async def _step_button(modal: Locator) -> tuple[Locator | None, str]:
    """The step's forward button and its label ("submit application", "review", "next", ...)."""
    for button in await modal.locator("button").all():
        try:
            if not await button.is_visible():
                continue
            label = re.sub(r"\s+", " ", (await button.inner_text()).strip().lower())
            aria = (await button.get_attribute("aria-label") or "").strip().lower()
        except PlaywrightError:
            continue
        for word in _NEXT_WORDS:
            if label == word or aria.startswith(word):
                return button, word
    return None, ""


async def _errors(page: Page) -> list[str]:
    try:
        texts = await page.locator(_ERRORS).all_inner_texts()
    except PlaywrightError:
        return []
    return list(dict.fromkeys(t.strip() for t in texts if t.strip()))[:5]


async def _unfollow(page: Page) -> None:
    """Untick "Follow <company> to stay up to date" (ticked by LinkedIn by default)."""
    with contextlib.suppress(PlaywrightError):
        box = page.locator(
            "input[type='checkbox'][id*='follow' i]:checked, "
            "input[type='checkbox'][aria-label^='follow' i]:checked"
        )
        if await box.count():
            await box.first.uncheck(force=True)


async def open_easy_apply(page: Page, url: str) -> None:
    """Open the job and its Easy Apply dialog. Raises ``FillError`` (or a subclass) when it cannot."""
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_load_state("networkidle", timeout=10_000)
    if _LOGIN.search(page.url):
        raise LoginNeededError("LinkedIn needs you to log in once: run `uv run sarthi login linkedin`")
    text = await _text(page)
    if _LIMIT.search(text):
        raise LimitReachedError("LinkedIn's daily Easy Apply limit is reached")
    button = page.locator('button[aria-label*="Easy Apply"], a[aria-label*="Easy Apply"]')
    visible = [b for b in await button.all() if await b.is_visible()]
    if not visible:
        if _ALREADY.search(text):
            raise FillError("LinkedIn shows this application as already submitted")
        if closed := adapters.CLOSED_TEXT.search(text):
            raise adapters.PostingClosedError(f"the posting is closed (LinkedIn: {closed.group(0)})")
        company_site = await _company_site_button(page)
        if company_site is not None and (target := await external.follow(page, company_site, is_linkedin)):
            raise external.ExternalApplyError(target)
        raise FillError("this job has no Easy Apply (it applies on the company's own site)")
    await visible[0].click()
    with contextlib.suppress(PlaywrightError):  # "Continue applying" after a saved draft
        cont = page.locator('button:has-text("Continue applying"), a:has-text("Continue applying")')
        if await cont.count():
            await cont.first.click(timeout=2000)


async def _company_site_button(page: Page) -> Locator | None:
    """The plain "Apply" of a job that applies on the company's own site (never an Easy Apply button)."""
    for candidate in await page.locator(COMPANY_SITE).all():
        try:
            if not await candidate.is_visible():
                continue
            words = f"{await candidate.get_attribute('aria-label') or ''} {await candidate.inner_text()}"
        except PlaywrightError:
            continue
        if "easy apply" not in words.lower():
            return candidate
    return None


async def walk(page: Page, ctx: FormContext, resume_file: str) -> StepResult:
    """Fill each step and move on, stopping at the "Submit application" step (never clicking it)."""
    result = StepResult()
    seen: set[str] = set()
    for _ in range(MAX_STEPS):
        modal = await _modal(page)
        if modal is None:
            result.note = "the Easy Apply dialog did not open"
            return result
        await _fill_step(page, ctx, resume_file, result, seen)
        if result.blanks or result.failed:
            return result
        button, word = await _step_button(modal)
        if button is None:
            result.note = "no Next / Review / Submit button in the dialog"
            return result
        if word == "submit application":
            await _unfollow(page)
            result.ready = True
            return result
        await button.click()
        await asyncio.sleep(STEP_PAUSE_S)
        if errors := await _errors(page):
            result.failed += [("LinkedIn", e) for e in errors]
            return result
    result.note = f"more than {MAX_STEPS} steps"
    return result


_DISPLAYED_JS = "e => !!(e.offsetParent || e.getClientRects().length)"


async def _displayed(page: Page, form_field: FormField) -> bool:
    """On the current step (fields of later steps are in the page but not displayed yet)."""
    element = page.locator(f"[{adapters.KEY_ATTR}='{form_field.key}']").first
    try:
        return bool(await element.evaluate(_DISPLAYED_JS))
    except PlaywrightError:
        return False


async def _fill_step(
    page: Page, ctx: FormContext, resume_file: str, result: StepResult, seen: set[str]
) -> None:
    discovered = await adapters.discover(page, LINKEDIN)
    fields = [f for f in discovered if f.type == "file" or await _displayed(page, f)]
    for form_field in fields:
        if form_field.type == "file":
            if resume_file and "cover" not in form_field.label.lower():
                with contextlib.suppress(PlaywrightError, FillError):
                    await adapters.fill_field(page, LINKEDIN, form_field, resume_file)
                    result.filled.append(("Resume", resume_file.rsplit("/", 1)[-1]))
            continue
        key = f"{form_field.label}|{form_field.type}"
        if key in seen:
            continue
        seen.add(key)
        if form_field.options and all(re.search(r"\.(pdf|docx?)$", o, re.I) for o in form_field.options):
            continue  # LinkedIn's saved-resume picker: the uploaded file above is selected by LinkedIn
        if await _already_answered(page, form_field):
            continue  # LinkedIn prefilled it from the profile (email, phone)
        suggestion = suggest(form_field, ctx)
        if suggestion is None:
            if form_field.required:
                result.blanks.append(form_field)
            continue
        try:
            await adapters.fill_field(page, LINKEDIN, form_field, suggestion.value)
        except (FillError, PlaywrightError) as error:
            if form_field.required:
                result.failed.append((form_field.label, str(error).splitlines()[0][:200]))
            continue
        result.filled.append((form_field.label, suggestion.value[:200]))


async def _already_answered(page: Page, form_field: FormField) -> bool:
    if form_field.type not in ("text", "email", "tel", "select", "textarea", "number"):
        return False
    element = page.locator(f"[{adapters.KEY_ATTR}='{form_field.key}']").first
    try:
        value = (await element.input_value()).strip()
    except PlaywrightError:
        return False
    return bool(value) and not re.fullmatch(r"select an option|select|--", value, re.I)
