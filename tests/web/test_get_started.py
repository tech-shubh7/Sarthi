"""A first start: the Overview says what is missing (an AI key, a profile) until both are there."""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from sarthi.web.app import create_app, read_only
from tests.conftest import FakeClock
from tests.web.test_settings import FakeActions


def _overview(conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock, actions: FakeActions) -> str:
    app = create_app(
        read_only(tmp_path / "data" / "sarthi.db"), clock=clock, actions=actions, data_dir=tmp_path
    )
    response = TestClient(app, base_url="http://127.0.0.1").get("/")
    assert response.status_code == 200
    return response.text


def test_a_new_install_is_told_to_add_a_key_and_a_profile(
    conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock
) -> None:
    actions = FakeActions(profile=None)
    actions.ai = False
    html = _overview(conn, tmp_path, clock, actions)
    assert "Get started" in html
    assert "Add an AI key." in html and "SARTHI_GEMINI_API_KEY=" in html
    assert "Add your profile." in html and 'href="/settings/profile"' in html


def test_only_what_is_still_missing_is_asked(
    conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock
) -> None:
    actions = FakeActions(profile=None)
    html = _overview(conn, tmp_path, clock, actions)
    assert "AI key added." in html and "Add your profile." in html


def test_a_set_up_sarthi_shows_no_checklist(
    conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock
) -> None:
    assert "Get started" not in _overview(conn, tmp_path, clock, FakeActions())
