"""Keep a site login alive after its browser window closes.

Shine, Naukri and others sign the user in with cookies that last "until the browser closes". Sarthi's login
window is an ordinary browser that the user closes, and closing it threw those cookies away: the user was
signed out again at once, every day (2026-11-28). Chromium's "continue where you left off" setting does not
keep them in this kind of browser (tested), so Sarthi keeps them itself.

While the login window is open, Sarthi reads the browser's cookies over its local DevTools port and gives
the site's "until the browser closes" cookies an expiry of ``KEEP_DAYS``, so the browser stores them on disk.
Only browser-wide Storage commands are sent: no page is attached and nothing runs inside a page, so the site
sees the same plain browser as before. The port listens on 127.0.0.1 only, and only while the window is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
from pathlib import Path
from typing import Any

import websockets

log = logging.getLogger(__name__)

KEEP_DAYS = 30
CHECK_EVERY_S = 1.0  # a user may close the window a moment after the site signs them in
PORT_FILE = (
    "DevToolsActivePort"  # Chromium writes "<port>\n<browser path>" here for --remote-debugging-port=0
)
PORT_ARG = "--remote-debugging-port=0"
_REPLY_S = 5.0


def site_domain(host: str) -> str:
    """The registrable part of a login host ("www.shine.com" -> "shine.com")."""
    return ".".join(host.split(".")[-2:])


def belongs(cookie_domain: str, domain: str) -> bool:
    """The cookie is the site's own: "shine.com" or a subdomain, never "notshine.com"."""
    host = cookie_domain.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


def _belongs(cookie: dict[str, Any], domain: str) -> bool:
    return belongs(str(cookie.get("domain", "")), domain)


def to_keep(cookies: list[dict[str, Any]], domain: str, until: float) -> list[dict[str, Any]]:
    """The site's "until the browser closes" cookies, as CDP CookieParams that last until ``until``."""
    kept: list[dict[str, Any]] = []
    for cookie in cookies:
        if not cookie.get("session") or not _belongs(cookie, domain):
            continue
        name, value = cookie.get("name"), cookie.get("value")
        if not name or value is None:
            continue
        host = str(cookie.get("domain", ""))
        param: dict[str, Any] = {
            "name": name,
            "value": value,
            "path": cookie.get("path") or "/",
            "secure": bool(cookie.get("secure")),
            "httpOnly": bool(cookie.get("httpOnly")),
            "expires": until,
        }
        if cookie.get("sameSite"):
            param["sameSite"] = cookie["sameSite"]
        if host.startswith("."):
            param["domain"] = host
        else:  # a host-only cookie (a __Host- one too) stays host-only: set through its URL, no Domain
            scheme = "https" if param["secure"] else "http"
            param["url"] = f"{scheme}://{host}{param['path']}"
        kept.append(param)
    return kept


async def _endpoint(profile_dir: Path, wait_s: float = 15.0) -> str | None:
    """The browser's DevTools address, once Chromium has written it."""
    path = profile_dir / PORT_FILE
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        with contextlib.suppress(OSError, ValueError, IndexError):
            port, browser = path.read_text(encoding="utf-8").split("\n")[:2]
            if port.strip().isdigit() and browser.strip():
                return f"ws://127.0.0.1:{int(port)}{browser.strip()}"
        await asyncio.sleep(0.3)
    return None


class _Cdp:
    """Browser-level DevTools commands over one WebSocket (no page is ever attached)."""

    def __init__(self, socket: Any) -> None:
        self.socket = socket
        self._ids = itertools.count(1)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        sent = next(self._ids)
        await self.socket.send(json.dumps({"id": sent, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await asyncio.wait_for(self.socket.recv(), _REPLY_S))
            if message.get("id") == sent:
                if "error" in message:
                    raise RuntimeError(str(message["error"])[:200])
                return dict(message.get("result") or {})


async def keep_while_open(window: asyncio.subprocess.Process, profile_dir: Path, domain: str) -> int:
    """Until ``window`` exits, keep ``domain``'s session cookies for ``KEEP_DAYS``; how many were kept."""
    closed = asyncio.ensure_future(window.wait())
    found = asyncio.ensure_future(_endpoint(profile_dir))
    kept_names: set[str] = set()
    try:
        await asyncio.wait({closed, found}, return_when=asyncio.FIRST_COMPLETED)
        url = found.result() if found.done() else None
        if url is None:
            log.info("login window: no DevTools port; session cookies are kept after the check instead")
            return 0
        async with websockets.connect(url, max_size=16 * 1024 * 1024, open_timeout=_REPLY_S) as socket:
            cdp = _Cdp(socket)
            while not closed.done():
                cookies = (await cdp.call("Storage.getCookies")).get("cookies") or []
                keep = to_keep(cookies, domain, time.time() + KEEP_DAYS * 86400)
                if keep:
                    await cdp.call("Storage.setCookies", {"cookies": keep})
                    kept_names |= {c["name"] for c in keep}
                await asyncio.wait({closed}, timeout=CHECK_EVERY_S)
    except (
        OSError,
        websockets.WebSocketException,
        RuntimeError,
        ValueError,
    ) as error:  # TimeoutError is an OSError
        # The user closed the window between two checks: what was kept so far stays kept.
        log.debug("login window: cookie keeper stopped: %s", type(error).__name__)
    finally:
        found.cancel()
        if not closed.done():
            closed.cancel()
    if kept_names:
        log.info("%s: kept %d login cookie(s) for %d days", domain, len(kept_names), KEEP_DAYS)
    return len(kept_names)
