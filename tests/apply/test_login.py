"""A site login opens a plain browser window (no automation attached) and checks the session afterwards."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sarthi.apply import engine


class Window:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.terminated = False

    async def wait(self) -> int:
        await asyncio.sleep(self.seconds)
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.seconds = 0


@pytest.mark.parametrize("recognised", [True, False])
async def test_login_window_is_a_plain_browser_and_the_check_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recognised: bool
) -> None:
    started: list[list[str]] = []
    window = Window(0.01)

    async def spawn(args: list[str], env: dict[str, str]) -> Window:
        started.append(args)
        return window

    checked: list[bool] = []

    async def check(site: str, profile_dir: Path, *, headless: bool = True) -> bool:
        assert site == "linkedin" and profile_dir == tmp_path / "profile"
        checked.append(headless)
        return recognised

    monkeypatch.setattr(engine, "_open_window", spawn)
    monkeypatch.setattr(engine, "signed_in", check)
    assert await engine.login("linkedin", tmp_path / "profile", headless=False) is recognised
    assert checked == [False]  # checked by the same kind of browser that applies (Shine refuses headless)
    args = started[0]
    assert f"--user-data-dir={tmp_path / 'profile'}" in args
    assert args[-1] == engine.LOGIN_PAGES["linkedin"]
    # No automation flags: LinkedIn answers an automated window with CAPTCHA after CAPTCHA. The only DevTools
    # switch is the cookie keeper's own: a random port on 127.0.0.1, while the window is open (see keeper).
    assert not any(flag in a for a in args for flag in ("enable-automation", "remote-debugging-address"))
    assert [a for a in args if "remote-debugging" in a] == ["--remote-debugging-port=0"]
    assert not any("webdriver" in a or "AutomationControlled" in a for a in args)


async def test_a_window_left_open_too_long_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    window = Window(60)

    async def spawn(*_: Any) -> Window:
        return window

    async def check(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(engine, "_open_window", spawn)
    monkeypatch.setattr(engine, "signed_in", check)
    assert not await engine.login("naukri", tmp_path / "profile", wait_s=0.05)
    assert window.terminated


@pytest.mark.parametrize(
    ("site", "address", "recognised"),
    [
        ("indeed", "https://profile.indeed.com/", True),
        ("indeed", "https://secure.indeed.com/auth", False),
        ("indeed", "https://indeed.example.com/profile/", False),
        ("linkedin", "https://www.linkedin.com/feed/", True),
        ("linkedin", "https://example.com/?next=https://www.linkedin.com/feed/", False),
        ("linkedin", "https://www.linkedin.com/login?next=/feed/", False),
        ("naukri", "https://www.naukri.com/mnjuser/homepage", True),
    ],
)
async def test_login_check_validates_the_host_and_final_path(
    site: str, address: str, recognised: bool
) -> None:
    class Page:
        url = address

        async def evaluate(self, expression: str) -> bool:
            return False  # no visible password field

    assert await engine._signed_in(Page(), site) is recognised  # type: ignore[arg-type]


async def test_a_closed_application_browser_is_started_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed window failed every later application ("browser has been closed"): one fresh start instead."""

    class Closed:
        async def new_page(self) -> Any:
            raise engine.PlaywrightError("Target page, context or browser has been closed")

    class Fresh:
        async def new_page(self) -> Any:
            class Page:
                def set_default_timeout(self, ms: int) -> None:
                    pass

            return Page()

    browser = engine.ApplyEngine(tmp_path, url_check=None)
    browser._context = Closed()  # type: ignore[assignment]
    resets: list[bool] = []

    async def reset() -> None:
        resets.append(True)
        browser._context = Fresh()  # type: ignore[assignment]

    monkeypatch.setattr(browser, "_reset_browser", reset)
    page = await browser._new_page()
    assert page is not None and resets == [True]


FLAGS = {"httpOnly": True, "secure": True, "sameSite": "Lax"}


class Cookies:
    """A browser context's cookie jar."""

    def __init__(self, cookies: list[dict[str, Any]]) -> None:
        self.jar = cookies
        self.added: list[dict[str, Any]] = []

    async def cookies(self) -> list[dict[str, Any]]:
        return self.jar

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.added += cookies


async def test_a_session_only_login_cookie_is_kept_for_thirty_days() -> None:
    """Shine signs in with a cookie that ends with the browser: the login was lost when its window closed."""
    jar = Cookies(
        [
            {
                "name": "sessionid",
                "value": "x",
                "domain": "www.shine.com",
                "path": "/",
                "expires": -1,
                **FLAGS,
            },
            {
                "name": "_ga",
                "value": "y",
                "domain": ".shine.com",
                "path": "/",
                "expires": 1900000000,
                **FLAGS,
            },
            {"name": "other", "value": "z", "domain": ".example.com", "path": "/", "expires": -1, **FLAGS},
        ]
    )
    await engine._keep_session(jar, "shine")  # type: ignore[arg-type]
    assert [c["name"] for c in jar.added] == ["sessionid"]  # only the site's own session-only cookies
    assert jar.added[0]["expires"] > 1_700_000_000


@pytest.mark.parametrize(
    ("title", "text", "bot_check"),
    [
        ("Just a moment...", "", True),  # LinkedIn's challenge to a hidden browser
        ("Blocked - Indeed.com", "", True),
        ("Indeed", "Request Blocked. Cloudflare Ray ID ...", True),
        ("Feed | LinkedIn", "Start a post", False),
        ("Sign in | LinkedIn", "Email or phone Password", False),
    ],
)
async def test_a_bot_check_page_is_not_taken_as_signed_out(title: str, text: str, bot_check: bool) -> None:
    class Page:
        async def title(self) -> str:
            return title

        async def evaluate(self, expression: str) -> str:
            return text

    assert await engine._behind_bot_check(Page()) is bot_check  # type: ignore[arg-type]
