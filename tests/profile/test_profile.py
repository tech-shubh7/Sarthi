import stat
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sarthi.profile.models import Preferences, Profile
from sarthi.profile.store import load_profile, profile_path, save_profile


def prefs(**overrides: Any) -> Preferences:
    data: dict[str, Any] = {"roles": ["Backend Engineer"], "home_country": "IN"}
    return Preferences.model_validate(data | overrides)


def test_home_is_always_a_work_and_visa_free_country() -> None:
    p = prefs(countries=["GB"], authorized_countries=["AE"])
    assert p.work_countries == {"IN", "GB"}
    assert p.visa_free_countries == {"IN", "AE"}


@pytest.mark.parametrize(
    "bad",
    [
        {"roles": []},
        {"home_country": "India"},
        {"countries": ["gb"]},
        {"years_experience": -1},
        {"min_salary": {"currency": "INR"}},
        {"unknown_field": 1},
    ],
)
def test_invalid_preferences_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        prefs(**bad)


def test_version_changes_with_the_profile() -> None:
    a = Profile(preferences=prefs())
    b = Profile(preferences=prefs(keywords=["python"]))
    assert a.version == Profile(preferences=prefs()).version
    assert a.version != b.version


def test_save_and_load_is_atomic_and_private(tmp_path: Path) -> None:
    path = profile_path(tmp_path / "data")
    assert load_profile(path) is None
    profile = Profile(preferences=prefs(), skills=("Python",), resume_text="Built payment APIs.")
    save_profile(path, profile)
    assert load_profile(path) == profile
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["profile.json"]  # no temp files left behind


def test_a_failed_save_keeps_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = profile_path(tmp_path)
    original = Profile(preferences=prefs())
    save_profile(path, original)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.fsync", broken)
    with pytest.raises(OSError, match="disk full"):
        save_profile(path, Profile(preferences=prefs(keywords=["go"])))
    assert load_profile(path) == original
    assert [p.name for p in tmp_path.iterdir()] == ["profile.json"]
