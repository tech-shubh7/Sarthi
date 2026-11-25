"""Naukri one-click apply with the user's own logged-in session (Sarthi's own: no reference repo had it).

Naukri applies with the profile stored on Naukri, so there is no form to fill: Sarthi checks the user is
logged in and that the job was not applied to already, then waits for the user's approval before clicking
Apply. A job with "Apply on company site" raises ``ExternalApplyError`` with the company's address instead,
and the application continues on that site. If Naukri opens its question chatbot after Apply, the job is
handed to the user, since those questions cannot be checked before they appear.
"""

from __future__ import annotations

import contextlib
import re

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from sarthi.apply import adapters, external
from sarthi.apply.adapters import Adapter, FillError
from sarthi.apply.linkedin import LoginNeededError

APPLY_BUTTON = "#apply-button, button.apply-button, button:has-text('Apply'):not(:has-text('company site'))"
NAUKRI = Adapter(
    name="naukri",
    root="body",
    container="div",
    label="label",
    submit=APPLY_BUTTON,
    success_text=re.compile(r"you have successfully applied|applied successfully|successfully applied", re.I),
    strict_success=True,
)
_COMPANY_SITE = re.compile(r"apply on company (site|website)", re.I)
COMPANY_SITE_BUTTON = (
    "#company-site-button, button:has-text('company site'), a:has-text('company site'), "
    "button:has-text('company website'), a:has-text('company website')"
)
_ALREADY = re.compile(r"\balready applied\b|^\s*applied\s*$", re.I | re.M)
_LOGGED_OUT = re.compile(r"\b(login|register)\b to apply|login to apply", re.I)
CHATBOT = "[class*='chatbot'], #_chatbot, .chatbot_DrawerContentWrapper"


def is_naukri(url: str) -> bool:
    """A Naukri job page."""
    return adapters.host_of(url).endswith("naukri.com")


async def open_job(page: Page, url: str) -> None:
    """Open the job and check it can be applied to on Naukri. Raises ``FillError`` when it cannot."""
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_load_state("networkidle", timeout=10_000)
    text = await adapters.body_text(page)
    if _LOGGED_OUT.search(text) or "nlogin" in page.url:
        raise LoginNeededError("Naukri needs you to log in once: run `uv run sarthi login naukri`")
    if _ALREADY.search(text):
        raise FillError("Naukri shows this job as already applied")
    company_site = [b for b in await page.locator(COMPANY_SITE_BUTTON).all() if await b.is_visible()]
    if company_site and (target := await external.follow(page, company_site[0], is_naukri)):
        raise external.ExternalApplyError(target)
    if company_site or _COMPANY_SITE.search(text):
        raise FillError("this job applies on the company's own site, and its link could not be read")
    button = page.locator(APPLY_BUTTON)
    if not [b for b in await button.all() if await b.is_visible()]:
        raise FillError("no Apply button on the Naukri page")


async def chatbot_open(page: Page) -> bool:
    """Naukri's question chatbot appeared after clicking Apply."""
    try:
        return any([await c.is_visible() for c in await page.locator(CHATBOT).all()])
    except PlaywrightError:
        return False
