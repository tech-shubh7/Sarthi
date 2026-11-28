"""Apply links that are only redirects, resolved to the employer's own page before Sarthi opens anything.

Freehire's apply links (``freehire.me/api/v1/go/<slug>``) answer a plain request with a 302 to the real
posting (Lever, Greenhouse, foundit, Shine ...). Opened in a browser, freehire.me refuses automated browsers,
so 45 of 61 auto-applies on 2026-11-28 stopped there without ever reaching the employer. One HTTP request
(no browser, no page rendered) gives the address the user would have landed on.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)

# host -> path prefix of its redirect links
REDIRECTORS: dict[str, str] = {"freehire.me": "/api/v1/go/"}
MAX_HOPS = 3
TIMEOUT_S = 15.0
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Sarthi/0.1 (+https://github.com/tech-shubh7/Sarthi)"
_TRACKING = ("utm_", "src", "ref", "source")


def is_redirect_link(url: str) -> bool:
    """True for a link that only forwards to the real posting."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    prefix = REDIRECTORS.get(host)
    return prefix is not None and parts.path.startswith(prefix)


def _clean(url: str) -> str:
    """The address without tracking parameters (utm_source=freehire.me and the like)."""
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


async def resolve(url: str, transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Where a redirect link leads (cleaned), or None when it could not be followed (kept as it was)."""
    current = url
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_S, transport=transport, headers={"User-Agent": USER_AGENT}
        ) as http:
            for _ in range(MAX_HOPS):
                if not is_redirect_link(current):
                    break
                response = await http.get(current, follow_redirects=False)
                location = response.headers.get("location")
                if not response.is_redirect or not location:
                    log.info("%s did not redirect (HTTP %s)", current, response.status_code)
                    return None
                current = str(httpx.URL(current).join(location))
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:  # a malformed Location included
        log.info("could not follow %s: %s", url, type(error).__name__)
        return None
    if is_redirect_link(current) or urlsplit(current).scheme not in ("http", "https"):
        return None
    return _clean(current)
