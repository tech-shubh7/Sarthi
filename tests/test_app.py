from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from sarthi.app import SetupError, build, next_digest_at
from sarthi.cli import app as cli
from sarthi.config import Settings, get_settings
from sarthi.db.tasks import TaskStatus
from tests.conftest import FakeClock

IST = timezone(timedelta(hours=5, minutes=30))


def test_next_digest_today_or_tomorrow() -> None:
    morning = datetime(2026, 11, 20, 9, 0, tzinfo=IST)
    evening = datetime(2026, 11, 20, 20, 0, tzinfo=IST)
    assert next_digest_at(morning, 19).astimezone(IST).hour == 19
    assert next_digest_at(morning, 19).astimezone(IST).date() == morning.date()
    assert next_digest_at(evening, 19).astimezone(IST).date() == morning.date() + timedelta(days=1)


def test_build_needs_a_bot_token(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="SARTHI_TELEGRAM_BOT_TOKEN"):
        build(Settings(_env_file=None, data_dir=tmp_path))


async def test_build_wires_the_worker_and_schedules_one_digest(tmp_path: Path, clock: FakeClock) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        telegram_bot_token=SecretStr("1:x"),
        llm_fast_models=[],
        llm_quality_models=[],
        watch_companies=["greenhouse/razorpay", "not-valid"],
    )
    sarthi = build(settings, clock=clock)
    try:
        assert set(sarthi.worker().handlers) == {"source_check", "match", "alert", "digest"}
        assert sarthi.bot.setup is not None
        assert set(sarthi.sources.planned) == {"boards:linkedin+indeed", "greenhouse:razorpay"}
        sarthi.schedule_digest()
        sarthi.schedule_digest()
        assert sarthi.queue.store.task_counts()[TaskStatus.PENDING] == 1
        assert not any(client.available for client in sarthi.ai_clients)
    finally:
        await sarthi.bot.api.aclose()
        for client in sarthi.ai_clients:
            await client.aclose()
        sarthi.conn.close()


def test_pair_command_prints_a_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARTHI_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["pair"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0
    assert "/start " in result.stdout
    assert "expires in 15 minutes" in result.stdout


def test_bot_command_explains_a_missing_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARTHI_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["run"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert "SARTHI_TELEGRAM_BOT_TOKEN" in result.output
