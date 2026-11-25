"""A job board's "Apply on company site": where the employer's own application is.

LinkedIn jobs without Easy Apply and Naukri's "Apply on company site" send the applicant on to the employer's
site. Sarthi reads where that button leads (its link, or the tab it opens, through the board's redirect) with
the user's logged-in browser and raises ``ExternalApplyError`` carrying that address; the apply task then
checks the address and fills the form there like any other job (same guards, same approval before Submit).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from sarthi.apply import adapters
from sarthi.apply.adapters import FillError

FOLLOW_WAIT_S = 12.0
CONTINUE_AFTER_S = 2.5  # nothing opened yet: a "share your profile?" dialog may be waiting for Continue
CLICK_TIMEOUT_MS = 10_000
# A dialog some boards show before leaving for the company site (LinkedIn: "Share your profile?").
CONTINUE = (
    '[role="dialog"] button:has-text("Continue"), [role="dialog"] a:has-text("Continue"), '
    '.artdeco-modal button:has-text("Continue")'
)
# Query parameters a board's redirect link carries the real address in (linkedin.com/redir/redirect?url=...).
_REDIRECT_PARAMS = ("url", "redirect", "redirectUrl", "redirect_url", "dest", "destination", "target", "u")
_WEB = ("http://", "https://")


class ExternalApplyError(FillError):
    """The job applies on the company's own site, at ``url`` (not yet checked as safe to open)."""

    def __init__(self, url: str) -> None:
        """``url``: where the board's company-site button leads."""
        super().__init__(f"this job applies on the company's own site ({adapters.host_of(url) or url})")
        self.url = url


def unwrap(url: str, board: Callable[[str], bool]) -> str:
    """The address behind a board's redirect link, else ``url`` as it is."""
    for _ in range(3):  # a redirect inside a redirect, at most
        if not board(url):
            return url
        try:
            query = parse_qs(urlparse(url).query)
        except ValueError:
            return url
        inner = next(
            (v[0] for k in _REDIRECT_PARAMS if (v := query.get(k)) and v[0].startswith(_WEB)),
            "",
        )
        if not inner:
            return url
        url = inner
    return url


def _away(url: str, board: Callable[[str], bool]) -> str:
    """``url`` (unwrapped) when it is a web address off the board, else ""."""
    target = unwrap(url, board)
    return target if target.startswith(_WEB) and not board(target) else ""


async def follow(page: Page, button: Locator, board: Callable[[str], bool]) -> str:
    """Where the board's company-site ``button`` leads ("" when it could not be told).

    A link's own address when it has one; else the button is pressed and the address read from the tab it
    opens (or from this page, when it navigates). Any tab opened is closed again: the form is opened afresh,
    once the address has been checked.
    """
    with contextlib.suppress(PlaywrightError):
        href = await button.get_attribute("href", timeout=2000)
        if href and (target := _away(urljoin(page.url, href), board)):
            return target
    context = page.context
    before = list(context.pages)
    try:
        await button.click(timeout=CLICK_TIMEOUT_MS)
    except PlaywrightError:
        return ""
    loop = asyncio.get_running_loop()
    start = loop.time()
    pressed_continue = False
    try:
        while loop.time() - start < FOLLOW_WAIT_S:
            opened = [p for p in context.pages if p not in before]
            for tab in [*opened, page]:
                if target := _away(tab.url, board):
                    return target
            if not opened and not pressed_continue and loop.time() - start > CONTINUE_AFTER_S:
                pressed_continue = True
                with contextlib.suppress(PlaywrightError):
                    dialog = page.locator(CONTINUE)
                    if await dialog.count():
                        await dialog.first.click(timeout=3000)
            await asyncio.sleep(0.25)
        return ""
    finally:
        for tab in [p for p in context.pages if p not in before]:
            with contextlib.suppress(PlaywrightError):
                await tab.close()
