"""A damaged profile file never stops Sarthi from starting (2026-11-28 fresh-install test)."""

import logging
from pathlib import Path

import pytest

from sarthi.profile.store import DAMAGED_FILE, load_profile


@pytest.mark.parametrize(
    "content",
    [
        b'{"preferences": {"roles": [], "home_country": "XX"',  # cut off
        b'{"preferences": {"roles": ["Nurse"]}}',  # no home country
        b"\xff\xfe not utf-8",
        b"",
    ],
)
def test_a_damaged_profile_is_kept_aside_and_read_as_none(
    tmp_path: Path, content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "profile.json"
    path.write_bytes(content)
    with caplog.at_level(logging.ERROR):
        assert load_profile(path) is None
        assert load_profile(path) is None  # said once, not on every task
    assert (tmp_path / DAMAGED_FILE).read_bytes() == content
    assert path.read_bytes() == content  # the user's file itself is never changed
    assert sum("is damaged" in r.message for r in caplog.records) == 1
