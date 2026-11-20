import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sarthi import __version__
from sarthi.cli import app
from sarthi.config import Settings
from sarthi.log import RedactingFilter, redact


def test_version_command_prints_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_settings_read_prefixed_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SARTHI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SARTHI_TELEGRAM_BOT_TOKEN", "123456:secret-value")
    settings = Settings(_env_file=None)
    assert settings.db_path == tmp_path / "sarthi.db"
    assert settings.telegram_bot_token is not None
    assert "secret-value" not in repr(settings)
    assert settings.telegram_bot_token.get_secret_value() == "123456:secret-value"


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        ("token 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ", "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"),
        ("key=sk-abcdefghijklmnopqrstuv", "sk-abcdefghijklmnopqrstuv"),
        ("Authorization: Bearer abc.def.ghijklmnop", "abc.def.ghijklmnop"),
        ("password: hunter2!", "hunter2!"),
        ("AIzaSyA1234567890abcdefghijklmnopqrstu", "AIzaSyA1234567890abcdefghijklmnopqrstu"),
    ],
)
def test_redact_masks_secrets(raw: str, leaked: str) -> None:
    assert leaked not in redact(raw)


def test_redact_keeps_normal_text() -> None:
    text = "Matched 12 jobs at Razorpay in Bengaluru"
    assert redact(text) == text


def test_filter_redacts_formatted_args() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "using %s", ("password=abc123",), None)
    RedactingFilter().filter(record)
    assert "abc123" not in record.getMessage()
