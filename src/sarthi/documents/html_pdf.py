# Adapted from Resume-Matcher (Apache-2.0), apps/backend/app/pdf.py (_launch_browser, _render_page_to_pdf,
# _resolve_pdf_format, _resolve_pdf_margins) @ 959e458. Copyright (c) Resume-Matcher contributors.
# Changed by Sarthi contributors: renders an HTML string (set_content) instead of a print URL, blocks every
# network request (fonts come inline), launches Playwright's own Chromium per document instead of keeping a
# shared browser, and steps the template's size down until the resume fits its page count.
"""HTML to PDF with headless Chromium: real text that reflows, a selectable text layer, clickable links.

The page is given as one HTML string with its fonts inline, so nothing is fetched: every request the page
tries is refused. Chromium waits for the fonts (``document.fonts.ready``), then prints with the paper size and
margins given. ``fit_pages`` makes the resume keep its page count: the template's ``--s`` scale is lowered a
step at a time (down to ``MIN_SCALE``) until the PDF has no more pages than that.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from playwright._impl._api_structures import PdfMargins
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pypdf import PdfReader
from pypdf.errors import PdfReadError

log = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 30_000
SCALES = (1.0, 0.97, 0.94, 0.91, 0.88, 0.85)
MIN_SCALE = SCALES[-1]
PAPERS = {"a4": "A4", "us-letter": "Letter", "letter": "Letter"}


class HtmlPdfError(RuntimeError):
    """Chromium could not make the PDF."""


@dataclass(frozen=True, slots=True)
class Page:
    """Paper and margins (in points) for a printed document."""

    paper: str = "a4"  # "a4" or "us-letter" (Typst's names, used across Sarthi)
    top: float = 36.0
    right: float = 36.0
    bottom: float = 36.0
    left: float = 36.0

    @property
    def pdf_format(self) -> str:
        """Chromium's paper name."""
        return PAPERS.get(self.paper.lower(), "A4")

    def margins(self) -> PdfMargins:
        """Margins as Chromium takes them (it reads px, in, cm and mm; 72 points to the inch)."""

        def inches(points: float) -> str:
            return f"{points / 72:.4f}in"

        return {
            "top": inches(self.top),
            "right": inches(self.right),
            "bottom": inches(self.bottom),
            "left": inches(self.left),
        }


def page_count(pdf: bytes) -> int:
    """The number of pages in ``pdf`` (0 when it cannot be read)."""
    try:
        return len(PdfReader(io.BytesIO(pdf)).pages)
    except (PdfReadError, ValueError):
        return 0


async def html_to_pdf(html: str, page: Page, *, fit_pages: int | None = None) -> bytes:
    """``html`` printed by Chromium; with ``fit_pages``, made smaller until it has at most that many pages.

    When even the smallest step is longer, the full-size PDF is returned (a long resume stays readable).
    """
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                tab = await browser.new_page()
                await tab.route(
                    "**/*", lambda route: route.abort()
                )  # the page needs nothing from the network
                await tab.set_content(html, wait_until="load", timeout=NAV_TIMEOUT_MS)
                await tab.wait_for_function(
                    "() => document.fonts.ready.then(() => true)", timeout=NAV_TIMEOUT_MS
                )
                first = b""
                for scale in SCALES if fit_pages else (1.0,):
                    await tab.evaluate(
                        "s => document.documentElement.style.setProperty('--s', String(s))", scale
                    )
                    await tab.evaluate("() => document.fonts.ready")
                    pdf = await tab.pdf(format=page.pdf_format, margin=page.margins(), print_background=True)
                    first = first or pdf
                    if not fit_pages or page_count(pdf) <= fit_pages:
                        return pdf
                log.info(
                    "the resume is longer than %d page(s) even at %.0f%% size", fit_pages, MIN_SCALE * 100
                )
                return first
            finally:
                await browser.close()
    except PlaywrightError as error:
        message = str(error).splitlines()[0] if str(error) else type(error).__name__
        raise HtmlPdfError(f"Chromium could not print the resume: {message[:200]}") from error
