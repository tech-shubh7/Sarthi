"""The login screen: a real window where there is a desktop, else a virtual one shown in the browser."""

import shutil

import httpx
import pytest

from sarthi.apply import screen
from sarthi.apply.screen import ScreenError, login_screen


async def test_with_a_desktop_the_window_simply_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    async with login_screen() as shown:
        assert shown.url is None and shown.env is None


async def test_without_a_desktop_or_the_tools_it_says_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(screen.shutil, "which", lambda tool: None)
    with pytest.raises(ScreenError, match="Xvfb, x11vnc, websockify is not installed"):
        async with login_screen():
            pass


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in screen.TOOLS), reason="Xvfb, x11vnc and websockify not installed"
)
async def test_a_virtual_screen_is_served_with_a_password_and_stopped_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    async with login_screen(port=16080) as shown:
        assert shown.url is not None and shown.env is not None
        assert shown.url.startswith("http://127.0.0.1:16080/vnc.html?autoconnect=true")
        password = shown.url.rsplit("password=", 1)[1]
        assert len(password) == screen.VNC_PASSWORD_CHARS
        assert shown.env["DISPLAY"].startswith(":")
        async with httpx.AsyncClient() as http:
            page = await http.get("http://127.0.0.1:16080/vnc.html")
        assert page.status_code == 200
        with pytest.raises(ScreenError, match="in use"):  # a second login at the same time
            async with login_screen(port=16080):
                pass
    assert not screen._listening(16080)  # everything stopped
