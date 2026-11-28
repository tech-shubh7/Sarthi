"""Native Windows (and macOS): what differs from Linux must not stop Sarthi."""

from pathlib import Path

import pytest

from sarthi.apply import screen
from sarthi.profile import store
from sarthi.profile.models import Preferences, Profile


def test_the_profile_saves_without_fchmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(store.os, "fchmod")  # Windows has no os.fchmod
    profile = Profile(preferences=Preferences(roles=("Nurse",), home_country="GB"))
    store.save_profile(tmp_path / "profile.json", profile)
    assert store.load_profile(tmp_path / "profile.json") == profile


@pytest.mark.parametrize(("platform", "display", "expected"), [
    ("win32", "", True), ("darwin", "", True), ("linux", "", False), ("linux", ":0", True),
])  # fmt: skip
def test_windows_and_mac_always_have_a_screen_for_logins(
    monkeypatch: pytest.MonkeyPatch, platform: str, display: str, expected: bool
) -> None:
    monkeypatch.setattr(screen.sys, "platform", platform)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", display)
    assert screen.has_display() is expected
