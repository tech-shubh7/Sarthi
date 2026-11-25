"""New users get one-tap Telegram setup through the project's connector unless they turn it off."""

from pathlib import Path

import pytest

from sarthi.config import CONNECTOR_URL, Settings


def test_the_projects_connector_is_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SARTHI_TELEGRAM_CONNECT_URL")
    assert Settings(_env_file=None, data_dir=tmp_path).telegram_connect_url == CONNECTOR_URL
    assert CONNECTOR_URL.startswith("https://")


def test_an_empty_address_turns_telegram_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARTHI_TELEGRAM_CONNECT_URL", "")
    assert Settings(_env_file=None, data_dir=tmp_path).telegram_connect_url == ""
