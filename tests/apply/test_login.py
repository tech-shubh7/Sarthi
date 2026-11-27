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

    async def check(site: str, profile_dir: Path) -> bool:
        assert site == "linkedin" and profile_dir == tmp_path / "profile"
        return recognised

    monkeypatch.setattr(engine, "_open_window", spawn)
    monkeypatch.setattr(engine, "signed_in", check)
    assert await engine.login("linkedin", tmp_path / "profile") is recognised
    args = started[0]
    assert f"--user-data-dir={tmp_path / 'profile'}" in args
    assert args[-1] == engine.LOGIN_PAGES["linkedin"]
    assert not any("remote-debugging" in a or "enable-automation" in a for a in args)


async def test_a_window_left_open_too_long_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    window = Window(60)

    async def spawn(*_: Any) -> Window:
        return window

    async def check(*_: Any) -> bool:
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
