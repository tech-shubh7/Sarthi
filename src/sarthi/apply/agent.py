"""The application browser for any site: from a job page through to a filled form, stopping before Submit.

Hiring systems Sarthi has no adapter for (and job boards with the user's saved login) differ in layout, but
the steps are the same: get past a cookie banner, press "Apply", maybe choose "Apply manually", fill a page of
questions, press "Next", and so on until the last page. Each step:

1. **Fill** every field on the page from the profile and saved answers (``suggest``, as on known forms). A
   required question nobody has answered stops the walk: it goes to the user, like on any form.
2. **Rules first**: a cookie banner's "Accept", the one "Apply" button of a job page, the one "Next" of a form
   page, the final "Submit" (the walk is then ready: code never presses it, the user's approval does).
3. **AI only when the rules cannot tell** (``sarthi.ai.browser``): it picks one button from the page's list.

Whatever the AI says, code keeps the guards: no typing by the AI, no final press, no "Sign in with ...", no
leaving for an unrelated site, a login or sign-up page goes to the user, a CAPTCHA is never solved.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from sarthi.apply import adapters
from sarthi.apply.adapters import Adapter, FillError
from sarthi.jobs.forms import FormContext, FormField, Suggestion, suggest

log = logging.getLogger(__name__)

MAX_STEPS = 14
MAX_BUTTONS = 40
BUTTON_ATTR = "data-sarthi-btn"
SETTLE_MS = 8_000
NEW_TAB_WAIT_S = 2.5
CLICK_TIMEOUT_MS = 10_000
SLOW_PAGE_S = 10.0  # an empty page gets this long to show its content

AGENT = Adapter(
    name="ai",
    root="body",
    container="fieldset, .form-group, .field, .form-field, .question, li, [role=group]",
    label="",
    submit="",  # set per page: the key of the button the walk stopped at
)

# Button texts. Checked in this order: never, final, next, apply, cookie.
NEVER = re.compile(
    r"(?:sign|log)\s*(?:in|up)\b|with\s+(?:google|linkedin|facebook|apple|indeed|github|microsoft|seek)"
    r"|create\s+(?:an?\s+)?account|register|\bshare\b|save\s+(?:this\s+)?job|\bsaved?\b$|similar jobs"
    r"|tweet|whatsapp|facebook|twitter|instagram|youtube|privacy|terms|cookie\s+(?:settings|preferences)"
    r"|manage\s+(?:cookies|preferences)|reject|decline|language|english|deutsch|français|español"
    r"|withdraw|delete|remove|sign\s*out|log\s*out|\bupload",  # an upload button only opens a file dialog
    re.IGNORECASE,
)
FINAL = re.compile(
    r"^\s*(?:submit|send|finish|complete|confirm)\b|\bsubmit\b|send\s+(?:my\s+|your\s+)?application"
    r"|finish\s+application|complete\s+application",
    re.IGNORECASE,
)
NEXT = re.compile(
    r"^\s*(?:next|continue|proceed|review|save\s*(?:and|&)\s*continue|next\s+step|go\s+to\s+(?:the\s+)?next)\b",
    re.IGNORECASE,
)
APPLY = re.compile(
    r"\bapply\b|i'?m\s+interested|start\s+(?:your\s+|my\s+)?application|apply\s+manually|apply\s+now",
    re.IGNORECASE,
)
# A board's header when nobody is signed in ("Login", "Sign in", "Login / Register").
LOGGED_OUT = re.compile(
    r"^\s*(?:log\s*-?\s*in|sign\s*-?\s*in)(?:\s*(?:/|or|&)\s*(?:register|sign\s*up))?\s*$", re.I
)
SIGN_IN_TO_APPLY = re.compile(r"(?:sign|log)\s*-?\s*in\s+to\s+(?:apply|continue)", re.IGNORECASE)
COOKIE_OK = re.compile(
    r"^\s*(?:accept|accept\s+all|allow\s+all|i\s+agree|agree|got\s+it|ok(?:ay)?)\b", re.IGNORECASE
)
# Hiring systems a job page may send the applicant on to (an "Apply" that opens the employer's system).
ATS_HOSTS = re.compile(
    r"(?:^|\.)(?:greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|icims\.com|jobvite\.com"
    r"|bamboohr\.com|recruitee\.com|personio\.(?:de|com)|teamtailor\.com|breezy\.hr|applytojob\.com"
    r"|taleo\.net|successfactors\.(?:com|eu)|oraclecloud\.com|zohorecruit\.(?:com|in)|freshteam\.com"
    r"|darwinbox\.in|keka\.com|myworkdayjobs\.com|myworkdaysite\.com|rippling\.com|pinpointhq\.com"
    r"|jazzhr\.com|dover\.com|wellfound\.com)$",
    re.IGNORECASE,
)

BUTTONS_JS = """
(max) => {
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  // Into open shadow roots too: sites built from web components (SmartRecruiters) keep their buttons there.
  const deep = (root, sel, found = []) => {
    root.querySelectorAll(sel).forEach((e) => found.push(e));
    root.querySelectorAll("*").forEach((e) => { if (e.shadowRoot) deep(e.shadowRoot, sel, found); });
    return found;
  };
  deep(document, "[data-sarthi-btn]").forEach((e) => e.removeAttribute("data-sarthi-btn"));
  const out = [];
  let n = 0;
  const all = deep(document, "button, a[href], [role=button], input[type=submit], input[type=button]");
  for (const el of all) {
    if (!el.getClientRects().length) continue;
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none" || +st.opacity === 0) continue;
    // A faded-out or click-through copy (Shine's sticky "Apply Now" bar before it slides in): not pressable.
    if (st.pointerEvents === "none") continue;
    const seen = { opacityProperty: true, visibilityProperty: true };
    if (el.checkVisibility && !el.checkVisibility(seen)) continue;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
    const text = clean(el.innerText || el.value || el.getAttribute("aria-label") || el.title).slice(0, 80);
    if (!text) continue;
    const where = el.closest("form") ? "form"
      : el.closest("[role=dialog], dialog, [aria-modal=true]") ? "dialog" : "page";
    const key = "b" + (++n);
    el.setAttribute("data-sarthi-btn", key);
    out.push({ key, text, where, href: el.tagName === "A" ? el.href : "",
      type: (el.getAttribute("type") || "").toLowerCase() });
    if (out.length >= max) break;
  }
  return out;
}
"""
# A field is on screen when its control, or the label or box around it, is: styled forms hide the real radio
# or file input behind a nicer one, while a later step of a multi-page form hides the whole box.
SHOWN_JS = """
(key) => Array.from(document.querySelectorAll(`[data-sarthi-key="${key}"]`)).some((el) => {
  const box = el.closest("label, fieldset, .field, .form-group, .form-field, .question, li, [role=group]");
  return el.getClientRects().length > 0 || (box !== null && box.getClientRects().length > 0);
})
"""
SIGN_IN_FIELD_JS = """
() => Array.from(document.querySelectorAll("input[type=password]")).some((e) => e.getClientRects().length > 0)
"""
TEXT_JS = "() => (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().slice(0, 4000)"


class LoginWallError(FillError):
    """The site asks the user to sign in (or to create an account) before the application continues."""

    def __init__(self, message: str, *, account: bool = False) -> None:
        """``account``: applying needs a new account on this site, not just a sign-in."""
        super().__init__(message)
        self.account = account


@dataclass(frozen=True, slots=True)
class Button:
    """A button or link on the page."""

    key: str
    text: str
    where: str  # form, dialog or page
    href: str = ""
    type: str = ""

    @property
    def selector(self) -> str:
        """The CSS selector of this button on its page."""
        return f'[{BUTTON_ATTR}="{self.key}"]'


@dataclass(frozen=True, slots=True)
class Step:
    """What to do on a page: press a button to go on, or stop because the form is ready for Submit."""

    state: str  # go, ready, login, account, closed, stuck
    button: Button | None = None
    why: str = ""


# (page text, fields described, buttons described) -> the AI's (state, key, why), or None without an AI
Decide = Callable[[str, str, str], Awaitable[tuple[str, str, str] | None]]


@dataclass
class Walked:
    """Where the walk stopped."""

    page: Page
    submit: Button | None = None  # the final button, pressed only with the user's approval
    filled: list[tuple[str, str]] = field(default_factory=list)
    blanks: list[FormField] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    captcha: bool = False
    steps: list[str] = field(default_factory=list)  # what was pressed, for the log and the user
    note: str = ""
    known_form: str = ""  # reached Greenhouse, Lever or Ashby: their own adapter fills it from here

    def adapter(self) -> Adapter:
        """The adapter whose submit is the button the walk stopped at (its key, or its text as a fallback)."""
        if self.submit is None:
            return AGENT
        text = self.submit.text.replace('"', "'")
        fallback = f'button:has-text("{text}"), input[type=submit][value="{text}"]'
        return Adapter(
            name=AGENT.name,
            root=AGENT.root,
            container=AGENT.container,
            label=AGENT.label,
            submit=f"{self.submit.selector}, {fallback}",
            extra={"agent": True, "button": self.submit.selector},
        )


_SITE_PARTS = 2  # acme.com
_COUNTRY_CODE = 2  # .in, .uk
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac"})  # acme.co.in


def _site(host: str) -> str:
    """The registrable part of a host: careers.acme.com -> acme.com, jobs.acme.co.in -> acme.co.in."""
    parts = host.lower().split(".")
    two_part_suffix = (
        len(parts) > _SITE_PARTS and len(parts[-1]) == _COUNTRY_CODE and parts[-2] in _SECOND_LEVEL
    )
    return ".".join(parts[-(_SITE_PARTS + 1 if two_part_suffix else _SITE_PARTS) :])


def allowed_link(href: str, start: str, current: str) -> bool:
    """A link the walk may follow: the same site as the job or the page, or a hiring system."""
    if not href:
        return True
    try:
        host = (urlparse(href).hostname or "").lower()
        scheme = urlparse(href).scheme
    except ValueError:
        return False
    if scheme in ("", "javascript") or href.startswith("#"):
        return True  # handled on this page
    if scheme not in ("http", "https", "file"):
        return False  # mailto:, tel:, app links
    if scheme == "file":
        return True  # local test pages
    sites = {_site(urlparse(u).hostname or "") for u in (start, current)}
    return _site(host) in sites or bool(ATS_HOSTS.search(host))


def rule_step(  # noqa: PLR0911  (one return per kind of page)
    buttons: list[Button], fields: list[FormField], page_text: str, *, one_click: bool
) -> Step | None:
    """The step when the page makes it plain; None when only the AI can tell."""
    usable = [b for b in buttons if not NEVER.search(b.text)]
    if fields:
        in_form = [b for b in usable if b.where in ("form", "dialog")] or usable
        finals = [b for b in in_form if FINAL.search(b.text)]
        if len({b.text.lower() for b in finals}) == 1:
            return Step("ready", finals[0], "the form's submit button")
        nexts = [b for b in in_form if NEXT.search(b.text)]
        if len({b.text.lower() for b in nexts}) == 1:
            return Step("go", nexts[0], "the form's next page")
        return None
    # A banner is told by its buttons too ("Cookies settings"): it often sits past the text read.
    if re.search(r"cookie", page_text, re.IGNORECASE) or any("cookie" in b.text.lower() for b in buttons):
        oks = [b for b in usable if COOKIE_OK.search(b.text) and b.where in ("dialog", "page")]
        if oks:
            return Step("go", oks[0], "accept the cookie banner")
    applies = [b for b in usable if APPLY.search(b.text) and not FINAL.search(b.text)]
    if len({b.text.lower() for b in applies}) == 1:
        if one_click:  # a board with the user's login: its "Apply" may send the application at once
            return Step("ready", applies[0], "the board's apply button (may send at once)")
        return Step("go", applies[0], "the job page's apply button")
    return None


# Text a label picks up that is not the question: an icon's fallback, screen-reader hints.
_LABEL_NOISE = re.compile(
    r"svgs? (?:are )?not supported by this browser\.?|your browser does not support (?:svg|the video tag)\.?"
    r"|\(?required\)?$|\(?optional\)?$",
    re.IGNORECASE,
)


OTHER_UPLOAD = re.compile(r"cover|letter|photo|picture|portfolio|transcript", re.IGNORECASE)


def readable(form_field: FormField) -> FormField:
    """The field with a label a person (and ``suggest``) can read: noise gone, "cLocation" -> "Location"."""
    label = _LABEL_NOISE.sub(" ", form_field.label).strip(" :-")
    if label and " " not in label:  # an id or name used as the label: candidateFirstName, c_location
        label = re.sub(r"(?<=[a-z])(?=[A-Z])|[_\-.]+", " ", label)
        label = re.sub(r"^(?:[a-z] |txt |fld |input |cand(?:idate)? )", "", label, flags=re.IGNORECASE)
        label = label.strip().capitalize() if label.islower() else label.strip()
    if label == form_field.label:
        return form_field
    return FormField(
        form_field.key, label or form_field.label, form_field.type, form_field.required, form_field.options
    )


def dead_end(url: str, fields: list[FormField], buttons: list[Button], text: str, *, one_click: bool) -> None:
    """Raise when the page cannot lead on.

    A blank page (a site that blocks automated browsers), or a board's "Apply" while its header still offers
    "Login" (the user is signed out there).
    """
    if not fields and not buttons and not text.strip():
        raise FillError(
            "the page came up empty (the site may block automated browsers): apply there yourself"
        )
    if not fields and adapters.BLOCKED_PAGE.search(text):
        # A firewall's page (Indeed's "Request Blocked", Cloudflare's checks): checked before the "Sign in"
        # link in its header, which once made it read as "signed out" and sent the user to log in again.
        raise FillError(adapters.blocked_reason(url))
    if one_click and not fields and any(LOGGED_OUT.search(b.text) for b in buttons):
        raise LoginWallError(f"{urlparse(url).hostname or 'the site'} shows you as signed out")
    # A guest's way in, and nothing else: "Upload your resume, we'll auto-fill your details" (a new account
    # made from the resume) next to "Already on Shine? Sign in to apply". Not an application form.
    usable_apply = any(APPLY.search(b.text) and not NEVER.search(b.text) for b in buttons)
    if not fields and not usable_apply and any(SIGN_IN_TO_APPLY.search(b.text) for b in buttons):
        raise LoginWallError(
            f"{urlparse(url).hostname or 'the site'} shows you as signed out (it offers only "
            '"Sign in to apply" or a new account made from your resume)'
        )


def guarded(step: Step, *, one_click: bool) -> Step:
    """An AI step held to the rules: never a forbidden button; a final press becomes "ready"."""
    button = step.button
    if step.state not in ("go", "ready") or button is None:
        return step
    if NEVER.search(button.text):
        return Step("stuck", None, f"the only way on was {button.text!r}, which Sarthi never presses")
    if step.state == "go" and (FINAL.search(button.text) or (one_click and APPLY.search(button.text))):
        return Step("ready", button, "that press would send the application")
    return step


def describe_fields(fields: list[FormField], filled: dict[str, str]) -> str:
    """Fields for the AI: label, type, required, filled or empty (never the values)."""
    lines = []
    for f in fields[:40]:
        state = "filled" if f.label in filled else "EMPTY"
        lines.append(f"{f.label[:80]} | {f.type}{' | required' if f.required else ''} | {state}")
    return "\n".join(lines)


def describe_buttons(buttons: list[Button]) -> str:
    """Buttons for the AI: key | text | where (and the link's host)."""
    lines = []
    for b in buttons:
        host = urlparse(b.href).hostname if b.href.startswith("http") else ""
        lines.append(f"{b.key} | {b.text} | {b.where}{f' | link to {host}' if host else ''}")
    return "\n".join(lines)


class ApplicationBrowser:
    """Walks one application on one page (and the tabs it opens)."""

    def __init__(self, decide: Decide | None, *, max_steps: int = MAX_STEPS) -> None:
        """``decide`` asks the AI when the rules cannot tell; None: rules only."""
        self._decide = decide
        self._max_steps = max_steps

    async def walk(self, page: Page, start: str, ctx: FormContext, *, one_click: bool = False) -> Walked:  # noqa: PLR0912, PLR0915  (each way a walk can stop)
        """From ``page`` (already at ``start``) to the filled last page of the form, or to why not.

        ``one_click``: a job board with the user's login, where "Apply" may send the application at once.
        Raises ``LoginWallError`` at a sign-in or sign-up page and ``FillError`` when the walk gets nowhere.
        """
        walked = Walked(page)
        filled: dict[str, str] = {}
        seen: dict[str, int] = {}
        for _ in range(self._max_steps):
            page = walked.page
            await self._settle(page)
            if (await adapters.detect_captcha(page)).get("interactive"):
                walked.captcha = True
                return walked
            if await page.evaluate(SIGN_IN_FIELD_JS):
                account = bool(
                    re.search(r"create (?:an )?account|sign ?up|register", await self._text(page), re.I)
                )
                raise LoginWallError(
                    f"{urlparse(page.url).hostname or 'the site'} asks you to "
                    + ("create an account" if account else "sign in")
                    + " before applying",
                    account=account,
                )
            fields = [f for f in await adapters.discover(page, AGENT) if await page.evaluate(SHOWN_JS, f.key)]
            await self._fill(page, fields, ctx, walked, filled)
            if walked.blanks or walked.failed:
                return walked  # a question only the user can answer (or a field that would not take a value)
            fields, buttons, text = await self._read(page, fields)
            signature = f"{page.url}|{len(fields)}|{'/'.join(b.text for b in buttons[:20])}"
            seen[signature] = seen.get(signature, 0) + 1
            if seen[signature] > 2:  # noqa: PLR2004  (the same page a third time: the press does nothing)
                raise FillError(
                    "the application did not move on after pressing "
                    + (walked.steps[-1] if walked.steps else "anything")
                )
            dead_end(page.url, fields, buttons, text, one_click=one_click)
            step = rule_step(buttons, fields, text, one_click=one_click)
            if step is None or (seen[signature] > 1 and step.state == "go"):
                step = await self._ask(text, fields, filled, buttons, one_click=one_click)
            if step.state == "ready" and step.button is not None:
                if not walked.filled and not one_click:
                    # A Submit-like button with nothing filled on the way (no form at all, often a closed
                    # posting, or no question matched): never offered for Submit.
                    if not fields:
                        raise FillError("no application form on the page (the job may be closed)")
                    raise FillError("none of the form's questions could be matched to your profile")
                walked.submit = step.button
                walked.note = step.why
                return walked
            if step.state in ("login", "account"):
                raise LoginWallError(
                    f"{urlparse(page.url).hostname or 'the site'} asks you to "
                    + ("create an account" if step.state == "account" else "sign in")
                    + " before applying",
                    account=step.state == "account",
                )
            if step.state == "closed":
                if adapters.CLOSED_TEXT.search(text):
                    raise adapters.PostingClosedError(
                        f"the posting is closed ({step.why or 'the site says so'})"
                    )
                raise FillError(f"the job is closed or this is not an application ({step.why or 'no form'})")
            if step.state != "go" or step.button is None:
                if one_click and not fields:
                    # A board shows a guest no usable Apply (Shine offers only "Upload your resume"): most
                    # often the user is signed out there.
                    raise LoginWallError(
                        f"{urlparse(page.url).hostname or 'the site'} showed no Apply button Sarthi can use; "
                        "you are probably signed out there"
                    )
                if fields and not buttons:
                    raise FillError("the form has no button to go on")
                raise FillError(step.why or "nothing on the page leads to the application form")
            if not allowed_link(step.button.href, start, page.url):
                raise FillError(
                    f"the apply link leads to another site ({urlparse(step.button.href).hostname})"
                )
            if adapters.is_workday(step.button.href):
                raise LoginWallError(
                    "Workday forms need your own Workday account for this employer", account=True
                )
            walked.page = await self._press(page, step.button)
            walked.steps.append(step.button.text)
            log.info("application browser: pressed %r (%s)", step.button.text, step.why)
            frame_form = await self._form_in_frame(walked.page)
            if frame_form:
                await walked.page.goto(frame_form, wait_until="domcontentloaded")
            if adapters.adapter_for(walked.page.url) is not adapters.GENERIC:
                walked.known_form = walked.page.url
                return walked
        raise FillError(f"the form was not reached in {self._max_steps} steps")

    async def _read(self, page: Page, fields: list[FormField]) -> tuple[list[FormField], list[Button], str]:
        """The page's buttons and text (and fields again when it was still empty: a slow page, BambooHR)."""
        buttons, text = await self._buttons(page), await self._text(page)
        if not fields and not buttons and not text.strip():
            return await self._wait_for_content(page)
        return fields, buttons, text

    async def _wait_for_content(self, page: Page) -> tuple[list[FormField], list[Button], str]:
        """An empty page, read again once it shows something (or after ``SLOW_PAGE_S``)."""
        end = asyncio.get_running_loop().time() + SLOW_PAGE_S
        while asyncio.get_running_loop().time() < end:
            await asyncio.sleep(1.0)
            if (await self._text(page)).strip():
                await self._settle(page)
                break
        fields = [f for f in await adapters.discover(page, AGENT) if await page.evaluate(SHOWN_JS, f.key)]
        return fields, await self._buttons(page), await self._text(page)

    async def _ask(
        self,
        text: str,
        fields: list[FormField],
        filled: dict[str, str],
        buttons: list[Button],
        *,
        one_click: bool,
    ) -> Step:
        if self._decide is None:
            return Step("stuck", None, "the page needs a choice and no AI model is set up")
        answer = await self._decide(text, describe_fields(fields, filled), describe_buttons(buttons))
        if answer is None:
            return Step("stuck", None, "the AI did not answer")
        state, key, why = answer
        by_key = {b.key: b for b in buttons}
        if state in ("go", "ready") and key not in by_key:
            return Step("stuck", None, f"the AI named no button on the page ({key!r})")
        return guarded(Step(state, by_key.get(key), why), one_click=one_click)

    async def _fill(
        self, page: Page, fields: list[FormField], ctx: FormContext, walked: Walked, filled: dict[str, str]
    ) -> None:
        fields = [readable(f) for f in fields]
        ordered = [f for f in fields if f.type == "file"] + [f for f in fields if f.type != "file"]
        files = [f for f in fields if f.type == "file"]
        for form_field in ordered:
            suggestion = suggest(form_field, ctx)
            if suggestion is None and form_field is (files[0] if files else None) and ctx.resume_file:
                # The page's first upload, its label saying nothing (an icon's fallback text): the resume.
                suggestion = (
                    None if OTHER_UPLOAD.search(form_field.label) else Suggestion(ctx.resume_file, "file")
                )
            if suggestion is None:
                if form_field.required:
                    walked.blanks.append(form_field)
                continue
            if filled.get(form_field.label) == suggestion.value and form_field.type == "file":
                continue  # the same file on the same question: not uploaded twice
            try:
                await adapters.fill_field(page, AGENT, form_field, suggestion.value)
            except (FillError, PlaywrightError) as error:
                if form_field.required:
                    walked.failed.append((form_field.label, str(error).splitlines()[0][:200]))
                continue
            filled[form_field.label] = suggestion.value
            shown = suggestion.value.rsplit("/", 1)[-1] if form_field.type == "file" else suggestion.value
            if (form_field.label, shown[:200]) not in walked.filled:
                walked.filled.append((form_field.label, shown[:200]))
            if form_field.type == "file":
                await page.wait_for_timeout(800)

    @staticmethod
    async def _buttons(page: Page) -> list[Button]:
        raw: list[dict[str, Any]] = await page.evaluate(BUTTONS_JS, MAX_BUTTONS * 3)
        buttons = [Button(**{k: str(v) for k, v in item.items()}) for item in raw]

        # The likeliest first: form buttons, then apply/next/final texts, then the rest.
        def rank(b: Button) -> int:
            return (0 if b.where in ("form", "dialog") else 1) + (
                0 if APPLY.search(b.text) or NEXT.search(b.text) or FINAL.search(b.text) else 2
            )

        return sorted(buttons, key=rank)[:MAX_BUTTONS]

    @staticmethod
    async def _text(page: Page) -> str:
        with contextlib.suppress(PlaywrightError):
            return str(await page.evaluate(TEXT_JS))
        return ""

    @staticmethod
    async def _settle(page: Page) -> None:
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=SETTLE_MS)
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("networkidle", timeout=3_000)

    async def _press(self, page: Page, button: Button) -> Page:
        """Press ``button``; the page it leads to (a new tab when it opened one)."""
        target = page.locator(button.selector).first
        with adapters.popups(page) as opened:  # this page's own tabs, never another application's
            try:
                await target.click(timeout=CLICK_TIMEOUT_MS)
            except PlaywrightError:
                # Covered by an overlay (a consent backdrop, a sticky bar): press the element itself.
                try:
                    await target.evaluate("(el) => el.click()")
                except PlaywrightError as error:
                    raise FillError(
                        f"could not press {button.text!r}: {str(error).splitlines()[0][:150]}"
                    ) from error
            end = asyncio.get_running_loop().time() + NEW_TAB_WAIT_S
            while asyncio.get_running_loop().time() < end:
                if opened:
                    new = opened[-1]
                    await self._settle(new)
                    if not allowed_link(new.url, page.url, page.url):
                        await new.close()
                        raise FillError(
                            f"the apply button opened another site ({urlparse(new.url).hostname})"
                        )
                    return new
                await asyncio.sleep(0.2)
        return page

    @staticmethod
    async def _form_in_frame(page: Page) -> str:
        """The address of an embedded application form (a hiring system in an iframe), or ""."""
        for frame in page.frames[1:]:
            host = (urlparse(frame.url).hostname or "").lower()
            if ATS_HOSTS.search(host) and not adapters.is_workday(frame.url):
                return frame.url
        return ""
