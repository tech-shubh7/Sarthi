"""How each hiring system's application form is read, filled and submitted (rule-based, no AI).

Greenhouse, Lever and Ashby have dedicated layouts; any other form is read by its labels (``generic``). A
form on a company page that embeds Greenhouse in an iframe is driven as Greenhouse. Workday is not filled:
it needs a per-employer account and a multi-page wizard, so it goes to the user.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from importlib import resources
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from sarthi.jobs.forms import FieldType, FormField, best_option, best_place, exact_option, pick_options

_JS = resources.files("sarthi.apply")
SCAN_JS = (_JS / "scan.js").read_text(encoding="utf-8")
CAPTCHA_JS = (_JS / "captcha.js").read_text(encoding="utf-8")
KEY_ATTR = "data-sarthi-key"
_FIELD_TYPES = frozenset(
    {
        "text",
        "textarea",
        "email",
        "tel",
        "url",
        "number",
        "date",
        "select",
        "radio",
        "checkbox",
        "combobox",
        "file",
    }
)


class FillError(RuntimeError):
    """One field could not be filled (the message says why, for the user)."""


class PostingClosedError(FillError):
    """The site says the posting is gone or no longer takes applications."""


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def host_of(url: str) -> str:
    """The URL's host in lower case ("" for anything unreadable)."""
    return _host(url)


def is_greenhouse(url: str) -> bool:
    """A Greenhouse board or a company page carrying a Greenhouse job id."""
    if re.fullmatch(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io", _host(url)):
        return True
    return "gh_jid=" in (urlparse(url).query if url else "")


def is_lever(url: str) -> bool:
    """A Lever board."""
    return re.fullmatch(r"jobs(?:\.eu)?\.lever\.co", _host(url)) is not None


def is_ashby(url: str) -> bool:
    """An Ashby board."""
    return _host(url) == "jobs.ashbyhq.com"


def is_workday(url: str) -> bool:
    """A Workday career site (not filled automatically)."""
    return re.search(r"(?:^|\.)(?:myworkdayjobs|myworkdaysite)\.com$", _host(url)) is not None


@dataclass(frozen=True, slots=True)
class Adapter:
    """One hiring system's form layout."""

    name: str
    root: str
    container: str
    label: str
    submit: str
    skip: str = ""
    required: str = ""
    yesno: str = ""
    success_selector: str = ""
    success_url: re.Pattern[str] | None = None
    success_text: re.Pattern[str] | None = None
    strict_success: bool = False  # only the success marks count (no generic "thank you" text)
    search_label: re.Pattern[str] | None = None  # comboboxes that search a huge list (Greenhouse "School")
    extra: dict[str, Any] = field(default_factory=dict)

    def scan_config(self) -> dict[str, str]:
        """The scanner's selectors."""
        return {
            "root": self.root,
            "container": self.container,
            "labelSel": self.label,
            "skip": self.skip,
            "requiredSel": self.required,
            "yesno": self.yesno,
        }


GREENHOUSE = Adapter(
    name="greenhouse",
    root="#application-form, #application_form, form",
    container=".field-wrapper, .eeoc__question__wrapper, .phone-input__country, .phone-input__phone, "
    ".file-upload, #application_form .field, fieldset",
    label=".upload-label, label.label, label.select__label, legend:not(.visually-hidden)",
    skip=".iti__dropdown-content, .iti__search-input",
    submit='#application-form button[type="submit"], #application_form #submit_app, button[type="submit"]',
    success_selector=".application--confirmation, #application_confirmation, [data-testid='confirmation']",
    success_url=re.compile(r"/confirmation\b|/thank[-_]?you", re.I),
    search_label=re.compile(r"^school$", re.I),
)
LEVER = Adapter(
    name="lever",
    root="#application-form, form",
    container="li.application-question, .application-additional",
    label=".application-label .text, .application-label",
    skip="#hcaptchaResponseInput, .h-captcha",
    submit="#btn-submit, button[data-qa='btn-submit']",
    success_url=re.compile(r"/thanks/?(\?|#|$)"),
    success_text=re.compile(r"application submitted!", re.I),
    strict_success=True,
)
ASHBY = Adapter(
    name="ashby",
    root="#form, .ashby-application-form-container",
    container=".ashby-application-form-field-entry, fieldset.ashby-application-form-input-radio-group, "
    "fieldset.ashby-application-form-input-checkbox-group",
    label=".ashby-application-form-question-title",
    skip="[class*='ashby-application-form-autofill'], [name='communicationConsent']",
    required=".ashby-application-form-question-title[class*='_required']",
    yesno=".ashby-application-form-input-yesno",
    submit=".ashby-application-form-submit-button",
    success_selector=".ashby-application-form-success-container",
)
GENERIC = Adapter(
    name="generic",
    root="form",
    container="fieldset, .form-group, .field, .form-field, li",
    label="",
    submit='form button[type="submit"], form input[type="submit"], button:has-text("Submit")',
)
ADAPTERS = {a.name: a for a in (GREENHOUSE, LEVER, ASHBY, GENERIC)}
_GREENHOUSE_FRAME = (
    '#grnhse_app iframe, iframe[src*="greenhouse.io/embed/job_app"], '
    'iframe[src*="greenhouse.io/embed/job_board"]'
)
LEVER_VERIFY_ERROR = re.compile(r"there was an error verifying your application", re.I)
# A closed posting's own words (the liveness idea of JobRadar, MIT: only a definite answer counts). Read only
# on a page where no form was found, so a description that mentions a filled position changes nothing.
CLOSED_TEXT = re.compile(
    r"no longer accepting applications|not (?:currently )?accepting applications"
    r"|(?:position|role|job|vacancy) has (?:already )?been filled"
    r"|\bis no longer (?:open|available|active|accepting)"
    r"|this (?:job|posting|position) (?:posting )?(?:has )?(?:expired|closed)|job not found"
    r"|(?:posting|job) (?:has been )?closed",
    re.IGNORECASE,
)
# Not "thank you for your interest": careers pages print it in their footer, next to an unsent form.
THANKS = re.compile(
    r"thank you for (applying|your application|submitting)|application (has been |was )?"
    r"(submitted|received)|we('ve| have) received your application|successfully submitted",
    re.I,
)


async def form_gone(page: Page, adapter: Adapter) -> bool:
    """The form is no longer on screen: the button the walk stopped at, else the form root, is gone.

    New thank-you text at the same address counts as sent only then: a footer or a banner beside a form
    still on screen says nothing about the form.
    """
    selector = str(adapter.extra.get("button") or "") or adapter.root
    try:
        shown = [await m.is_visible() for m in await page.locator(selector).all()]
    except PlaywrightError:
        return False
    return not any(shown)


def adapter_for(url: str) -> Adapter:
    """The adapter for a form URL."""
    if is_greenhouse(url):
        return GREENHOUSE
    if is_lever(url):
        return LEVER
    if is_ashby(url):
        return ASHBY
    return GENERIC


def apply_url(url: str) -> str:
    """The page with the form: Lever's ``/apply`` and Ashby's ``/application`` pages."""
    clean = url.rstrip("/")
    if is_lever(url) and not clean.endswith("/apply"):
        return f"{clean}/apply"
    if is_ashby(url) and not clean.split("?")[0].endswith("/application"):
        base, _, query = clean.partition("?")
        return f"{base}/application" + (f"?{query}" if query else "")
    return url


async def refine_for_page(page: Page, adapter: Adapter) -> Adapter:
    """A company page embedding a Greenhouse form: open the form itself and drive it as Greenhouse."""
    if adapter is not GENERIC:
        return adapter
    try:
        src = await page.locator(_GREENHOUSE_FRAME).first.get_attribute("src", timeout=1500)
    except PlaywrightError:
        return adapter
    if not src:
        return adapter
    await page.goto(urljoin(page.url, src), wait_until="domcontentloaded")
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_load_state("networkidle", timeout=15_000)
    return GREENHOUSE


# ---- reading the form ------------------------------------------------------------------------------------


def _fields(raw: Any) -> list[FormField]:
    fields: list[FormField] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not item.get("key"):
            continue
        kind = str(item.get("type") or "text")
        field_type: FieldType = kind if kind in _FIELD_TYPES else "unknown"  # type: ignore[assignment]
        options = tuple(str(o) for o in item.get("options") or [] if str(o).strip())
        fields.append(
            FormField(
                str(item["key"]),
                str(item.get("label") or "")[:500],
                field_type,
                bool(item.get("required")),
                options,
            )
        )
    return fields


# React-select dropdowns are plain HTML until React attaches to them; typing before that does nothing.
_HYDRATED_JS = """() => {
  const boxes = Array.from(document.querySelectorAll('[role="combobox"]'))
    .filter(c => /select__/.test(c.className));
  return boxes.length === 0 || boxes.every(c => Object.keys(c).some(k => k.startsWith('__react')));
}"""
HYDRATE_TIMEOUT_MS = 15_000


async def prepare(page: Page, adapter: Adapter) -> None:
    """Get the form on screen and interactive (Ashby opens on an overview tab and loads its form late)."""
    if adapter is ASHBY:
        tab = page.locator("#job-application-form")
        if await tab.count():
            with contextlib.suppress(PlaywrightError):
                await tab.first.click()
        with contextlib.suppress(PlaywrightError):
            await page.locator(".ashby-application-form-field-entry").first.wait_for(timeout=20_000)
    with contextlib.suppress(PlaywrightError):
        await page.locator(adapter.root).first.wait_for(timeout=15_000)
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_function(_HYDRATED_JS, timeout=HYDRATE_TIMEOUT_MS)


async def discover(page: Page, adapter: Adapter) -> list[FormField]:
    """Every question on the form, in order, with its options (opening static dropdowns to read them)."""
    await prepare(page, adapter)
    fields = _fields(await page.evaluate(SCAN_JS, adapter.scan_config()))
    out: list[FormField] = []
    for f in fields:
        if f.type == "combobox" and not (adapter.search_label and adapter.search_label.search(f.label)):
            options = await read_combo_options(page, page.locator(f"[{KEY_ATTR}='{f.key}']"))
            if options:
                f = FormField(f.key, f.label, f.type, f.required, tuple(options))  # noqa: PLW2901
        out.append(f)
    return out


async def _visible_options(
    page: Page, element: Locator, option_selector: str, wait_s: float
) -> list[Locator]:
    end = time.monotonic() + wait_s
    while True:
        try:
            controls = await element.get_attribute("aria-controls")
        except PlaywrightError:
            controls = None
        scope = page.locator(f'[id="{controls}"]') if controls else page.locator("body")
        options = [o for o in await scope.locator(option_selector).all() if await o.is_visible()]
        if options or time.monotonic() > end:
            return options
        await asyncio.sleep(0.15)


async def _texts(options: list[Locator]) -> list[str]:
    return [re.sub(r"\s+", " ", await o.inner_text()).strip() for o in options]


async def read_combo_options(
    page: Page, element: Locator, option_selector: str = '[role="option"]'
) -> list[str]:
    """A static dropdown's options (empty for search-as-you-type lists)."""
    try:
        await element.first.click(timeout=3000)
    except PlaywrightError:
        return []
    texts = await _texts(await _visible_options(page, element.first, option_selector, 0.6))
    with contextlib.suppress(PlaywrightError):
        await element.first.press("Escape")
    return [t for t in texts if t]


# ---- filling ---------------------------------------------------------------------------------------------


async def fill_combobox(  # noqa: PLR0913  (the widget's knobs)
    page: Page,
    element: Locator,
    value: str,
    *,
    static: bool,
    option_selector: str = '[role="option"]',
    exact: bool = False,
) -> None:
    """Type into a dropdown or autocomplete and click the matching option; never a random one."""
    wait = 1.5 if static else 4.0
    attempts = [value, value]  # fill, then key by key (some widgets only react to real keystrokes)
    if not static and "," in value:
        attempts.append(value.split(",", maxsplit=1)[0])
    if static:
        attempts.append("")
    await element.click()
    options: list[Locator] = []
    for i, text in enumerate(attempts):
        if i in {1, 2}:  # typed key by key
            await element.fill("")
            await element.press_sequentially(text, delay=40)
        else:
            await element.fill(text)
        options = await _visible_options(page, element, option_selector, wait)
        if options:
            break
    texts = await _texts(options)
    pick = (
        exact_option(texts, value)
        if exact
        else (best_option(texts, value) if static else best_place(texts, value))
    )
    if pick is None:
        with contextlib.suppress(PlaywrightError):
            await element.fill("")
            await element.press("Escape")
        raise FillError(f'no option matching "{value}"' if texts else f'no suggestions for "{value}"')
    await options[texts.index(pick)].click()


_TRUTHY = re.compile(r"yes|y|true|1|checked|agree|i agree", re.I)


async def _fill_choice(controls: Locator, form_field: FormField, value: str) -> None:
    options = list(form_field.options)
    first = controls.first
    if await controls.count() == 1 and await first.evaluate("e => e.tagName !== 'INPUT'"):  # yes/no buttons
        pick = best_option(options, value)
        if pick is None:
            raise FillError(f'no option matching "{value}"')
        button = first.locator("button").nth(options.index(pick))
        if await button.get_attribute("aria-pressed") != "true":  # clicking again would clear it
            await button.click()
        return
    if form_field.type == "checkbox" and len(options) <= 1:
        indexes = [0] if _TRUTHY.fullmatch(value.strip()) else []
    elif form_field.type == "checkbox":
        indexes = [options.index(o) for o in pick_options(options, value)]
    else:
        pick = best_option(options, value)
        indexes = [options.index(pick)] if pick is not None else []
    if not indexes:
        raise FillError(f'no option matching "{value}"')
    for i in indexes:
        control = controls.nth(i)
        if await control.is_checked():
            continue
        try:
            await control.check(force=True)
        except PlaywrightError:
            control_id = await control.get_attribute("id")
            target = (
                controls.page.locator(f'label[for="{control_id}"]')
                if control_id
                else control.locator("xpath=ancestor::label[1]")
            )
            await target.click()


async def _fill_date(element: Locator, iso: str) -> None:
    if await element.get_attribute("type") == "date":
        await element.fill(iso)
        return
    typed = re.sub(r"^(\d{4})-(\d{2})-(\d{2})$", r"\2/\3/\1", iso)  # react-datepicker shows MM/DD/YYYY
    await element.fill(typed)
    await element.press("Escape")
    await element.blur()
    shown = await element.input_value()
    if shown != typed:
        raise FillError(f'the date picker shows "{shown}" after typing {typed}')


async def fill_field(page: Page, adapter: Adapter, form_field: FormField, value: str) -> None:
    """Put ``value`` into one field."""
    controls = page.locator(f"[{KEY_ATTR}='{form_field.key}']")
    element = controls.first
    if form_field.type == "file":
        await element.set_input_files(value)
    elif form_field.type == "select":
        await element.select_option(label=best_option(form_field.options, value) or value)
    elif form_field.type == "combobox":
        searched = (
            adapter.search_label is not None and adapter.search_label.search(form_field.label) is not None
        )
        await fill_combobox(
            page, element, value, static=bool(form_field.options) and not searched, exact=searched
        )
    elif form_field.type == "date":
        await _fill_date(element, value)
    elif form_field.type in ("radio", "checkbox"):
        await _fill_choice(controls, form_field, value)
    elif adapter is LEVER and await element.evaluate("e => e.classList.contains('location-input')"):
        await fill_combobox(page, element, value, static=False, option_selector=".dropdown-results > *")
    else:
        await element.fill(value)
    if adapter is ASHBY and form_field.type in ("text", "textarea", "email", "tel", "url", "number"):
        await element.blur()  # Ashby saves text answers on blur


# ---- pressing submit -------------------------------------------------------------------------------------

SUBMIT_FIND_S = 10.0  # a submit button may be enabled a moment after the last answer (validation)
SUBMIT_CLICK_MS = 8_000
CONSENT_BUTTON_ATTR = "data-sarthi-consent"
# A control the user can see and press: on the page, not faded out (a hidden sticky bar's twin of the real
# button), not click-through, not disabled.
_SUBMIT_STATE_JS = """(el) => {
  const st = getComputedStyle(el);
  const seen = el.getClientRects().length > 0 && st.visibility !== "hidden" && st.pointerEvents !== "none"
    && (!el.checkVisibility || el.checkVisibility({ opacityProperty: true, visibilityProperty: true }));
  const enabled = !el.disabled && el.getAttribute("aria-disabled") !== "true"
    && !el.closest("fieldset[disabled]");
  return { seen, enabled };
}"""
# What sits on top of the button's centre: "" when the button itself; else a short description, and whether
# it is a cookie or consent banner (then its least-sharing choice is marked for a real click).
_COVER_JS = """(el, [attr, okPattern, leastPattern]) => {
  el.scrollIntoView({ block: "center", inline: "center" });
  const r = el.getBoundingClientRect();
  const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  for (let n = el; n; n = n.parentNode || n.host) { if (n === hit) return { cover: "" }; }
  if (!hit || el.contains(hit)) return { cover: "" };
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const box = hit.closest('[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i], '
    + '[aria-label*="cookie" i], [aria-label*="consent" i], #CybotCookiebotDialog, .cc-window')
    || (/cookie|consent/i.test(hit.innerText || "") ? hit.closest("[role=dialog], dialog, div") : null);
  const desc = clean(hit.getAttribute("aria-label") || hit.innerText || hit.id || hit.tagName).slice(0, 80);
  if (!box) return { cover: desc, consent: false };
  const shown = (b) => b.getClientRects().length > 0 && !b.disabled;
  const buttons = Array.from(box.querySelectorAll("button, [role=button], a, input[type=button]"))
    .filter(shown);
  const text = (b) => clean(b.innerText || b.value || b.getAttribute("aria-label"));
  const pick = buttons.find((b) => new RegExp(leastPattern, "i").test(text(b)))
    || buttons.find((b) => new RegExp(okPattern, "i").test(text(b)));
  if (pick) pick.setAttribute(attr, "1");
  return { cover: desc, consent: true, dismiss: pick ? text(pick) : "" };
}"""
CONSENT_OK = r"^\s*(?:accept|accept all|allow all|i agree|agree|got it|ok(?:ay)?|close)\b"
CONSENT_LEAST = r"^\s*(?:reject all|decline all|necessary only|only necessary|essential only|use necessary)"


async def _submit_target(page: Page, selector: str, prefer: str) -> tuple[Locator | None, bool]:
    """The first submit control the user can see and press (``prefer`` first), waiting a little for one.

    Returns (the control or None, whether a visible one was there but disabled).
    """
    end = time.monotonic() + SUBMIT_FIND_S
    disabled = False
    while True:
        candidates = (await page.locator(prefer).all() if prefer else []) + await page.locator(selector).all()
        for candidate in candidates:
            try:
                state = await candidate.evaluate(_SUBMIT_STATE_JS)
            except PlaywrightError:
                continue  # gone while looking
            if state.get("seen") and state.get("enabled"):
                return candidate, False
            disabled = disabled or bool(state.get("seen"))
        if time.monotonic() > end:
            return None, disabled
        await asyncio.sleep(0.3)


async def _cover(target: Locator) -> dict[str, Any]:
    try:
        found = await target.evaluate(_COVER_JS, [CONSENT_BUTTON_ATTR, CONSENT_OK, CONSENT_LEAST])
    except PlaywrightError:
        return {"cover": ""}
    return found if isinstance(found, dict) else {"cover": ""}


async def press_submit(page: Page, selector: str, *, prefer: str = "") -> tuple[str, str] | None:
    """Press the form's submit control like a person would; None when pressed, else (outcome, why).

    Hidden twins of the button (a faded sticky bar, a mobile copy) are skipped; a button disabled until the
    form validates gets a few seconds; a cookie or consent banner over it is dismissed (its least-sharing
    choice when it offers one). Nothing is force-clicked: anything else covering the button goes to the user.
    """
    target, disabled = await _submit_target(page, selector, prefer)
    if target is None:
        if disabled:
            return "needs_human", "The submit button stayed disabled: the form may still need an answer."
        return "failed", "No visible submit button was found on the form; check it on the site."
    for attempt in range(2):
        cover = await _cover(target)
        if cover.get("consent") and cover.get("dismiss"):
            with contextlib.suppress(PlaywrightError):
                await page.locator(f"[{CONSENT_BUTTON_ATTR}]").first.click(timeout=3_000)
                await page.wait_for_timeout(500)
        try:
            await target.click(timeout=SUBMIT_CLICK_MS)
        except PlaywrightError as error:
            if attempt == 0:
                continue
            what = str(cover.get("cover") or "")
            if what:
                return "needs_human", f'"{what}" covers the submit button on the page; submit it yourself.'
            return "failed", f"Could not press submit: {str(error).splitlines()[0][:200]}"
        return None
    return None


# ---- after submitting ------------------------------------------------------------------------------------


async def body_text(page: Page) -> str:
    """The page's visible text ("" when it cannot be read)."""
    try:
        return await page.locator("body").inner_text(timeout=2000)
    except PlaywrightError:
        return ""


async def confirmed(page: Page, adapter: Adapter) -> bool:
    """The hiring system's own success mark is on screen."""
    if adapter.success_url and adapter.success_url.search(page.url):
        return True
    if adapter.success_selector:
        marks = page.locator(adapter.success_selector)
        if any([await m.is_visible() for m in await marks.all()]):
            return True
    return bool(adapter.success_text and adapter.success_text.search(await body_text(page)))


async def rejected(page: Page, adapter: Adapter) -> str | None:
    """Why the hiring system refused a submission it was sent (Lever's verification banner), or None."""
    if (
        adapter is not LEVER
        or page.is_closed()
        or (adapter.success_url and adapter.success_url.search(page.url))
    ):
        return None
    text = await body_text(page)
    if adapter.success_text and adapter.success_text.search(text):
        return None
    if LEVER_VERIFY_ERROR.search(text):
        return 'Lever showed "There was an error verifying your application."'
    return None


async def detect_captcha(page: Page) -> dict[str, Any]:
    """{"interactive": bool, "invisible": bool, "kinds": [...]}; detection only, never solving."""
    if page.is_closed():
        return {"interactive": False, "invisible": False, "kinds": []}
    try:
        state = await page.evaluate(CAPTCHA_JS)
    except PlaywrightError:
        return {"interactive": False, "invisible": False, "kinds": []}
    return state if isinstance(state, dict) else {"interactive": False, "invisible": False, "kinds": []}
