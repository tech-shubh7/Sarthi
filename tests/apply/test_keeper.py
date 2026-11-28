"""A login made in the plain login window survives closing it (2026-11-28: users were signed out daily)."""

import asyncio
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from sarthi.apply import keeper


class _Site(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        if self.path == "/login":
            self.send_header("Set-Cookie", "sid=abc; Path=/; HttpOnly")  # "until the browser closes"
            self.send_header("Set-Cookie", "theme=dark; Path=/; Max-Age=600")  # already lasting
        self.end_headers()
        self.wfile.write(b"<html><body>signed in</body></html>")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def site() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_only_the_sites_session_cookies_are_kept() -> None:
    cookies = [
        {
            "name": "sid",
            "value": "1",
            "domain": "www.shine.com",
            "path": "/",
            "session": True,
            "secure": True,
        },
        {"name": "li", "value": "2", "domain": ".shine.com", "path": "/", "session": True, "sameSite": "Lax"},
        {"name": "keep", "value": "3", "domain": ".shine.com", "path": "/", "session": False},
        {"name": "other", "value": "4", "domain": ".example.com", "path": "/", "session": True},
        {"name": "lookalike", "value": "5", "domain": "notshine.com", "path": "/", "session": True},
    ]
    kept = keeper.to_keep(cookies, "shine.com", 2e9)
    assert [c["name"] for c in kept] == ["sid", "li"]
    host_only, domain_wide = kept
    assert host_only["url"] == "https://www.shine.com/" and "domain" not in host_only  # stays host-only
    assert domain_wide["domain"] == ".shine.com" and domain_wide["sameSite"] == "Lax"
    assert all(c["expires"] == 2e9 for c in kept)


async def test_a_login_window_closed_by_the_user_keeps_the_session(tmp_path: Path, site: str) -> None:
    # As login() does it: the browser's path is looked up, then the window runs with no Playwright attached.
    async with async_playwright() as playwright:
        executable = playwright.chromium.executable_path
    if not Path(executable).exists():
        pytest.skip("Chromium not installed for Playwright")
    profile = tmp_path / "profile"
    profile.mkdir()
    window = await asyncio.create_subprocess_exec(
        executable, f"--user-data-dir={profile}", "--headless=new", "--no-first-run", keeper.PORT_ARG,
        f"{site}/login", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    keeping = asyncio.create_task(keeper.keep_while_open(window, profile, "127.0.0.1"))
    await asyncio.sleep(10)  # the user signs in (slow under a full test run)...
    window.terminate()  # ...and closes the window
    await window.wait()
    assert await asyncio.wait_for(keeping, 10) >= 1
    async with async_playwright() as playwright:
        reopened = await playwright.chromium.launch_persistent_context(str(profile), headless=True)
        try:
            names = {c["name"] for c in await reopened.cookies(site)}
        finally:
            await reopened.close()
    assert "sid" in names  # without the keeper this was gone (tested: 0 of 4 ways kept it)
