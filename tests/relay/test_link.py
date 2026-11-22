"""Sarthi's side of the shared bot: registering once, switching bots safely, and the link to connect."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from sarthi import app as app_module
from sarthi.app import SetupError, build
from sarthi.chat.pairing import OWNER_KEY, code_hash, redeem
from sarthi.chat.telegram.bot import OFFSET_KEY
from sarthi.chat.telegram.link import LINK_FILE, MODE_KEY, PAIR_LINK_KEY, RelayError, relay_link
from sarthi.config import Settings
from sarthi.db.chat import ChatStore
from sarthi.db.connection import connect


def registrar(calls: list[str], body: object, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_registers_once_and_keeps_the_secret_private(tmp_path: Path) -> None:
    calls: list[str] = []
    http = registrar(calls, {"ok": True, "result": {"secret": "s3cret", "bot": "SarthiJobsBot"}})
    link = relay_link(tmp_path, "https://relay.example/", http)
    assert (link.url, link.secret.get_secret_value(), link.bot) == (
        "https://relay.example",
        "s3cret",
        "SarthiJobsBot",
    )
    assert calls == ["https://relay.example/v1/register"]
    assert stat.S_IMODE((tmp_path / LINK_FILE).stat().st_mode) == 0o600
    assert relay_link(tmp_path, "https://relay.example", http).secret.get_secret_value() == "s3cret"
    assert len(calls) == 1  # read back, not registered again
    assert link.pairing_link("AB12") == "https://t.me/SarthiJobsBot?start=AB12"
    assert "s3cret" not in repr(link)


def test_another_relay_or_a_broken_file_registers_again(tmp_path: Path) -> None:
    calls: list[str] = []
    http = registrar(calls, {"ok": True, "result": {"secret": "new", "bot": "B"}})
    (tmp_path / LINK_FILE).write_text(json.dumps({"url": "https://old.example", "secret": "old"}))
    assert relay_link(tmp_path, "https://relay.example", http).secret.get_secret_value() == "new"
    (tmp_path / LINK_FILE).write_text("{not json")
    assert relay_link(tmp_path, "https://relay.example", http).secret.get_secret_value() == "new"
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        ({"ok": False, "description": "Too Many Requests: retry after 3600"}, 429, "Too Many Requests"),
        ({"ok": True, "result": {}}, 200, "HTTP 200"),
        ([1, 2], 200, "HTTP 200"),
    ],
)
def test_a_refused_registration_says_why(tmp_path: Path, body: object, status: int, message: str) -> None:
    with pytest.raises(RelayError, match=message):
        relay_link(tmp_path, "https://relay.example", registrar([], body, status))
    assert not (tmp_path / LINK_FILE).exists()


def test_an_unreachable_relay_or_plain_http_is_an_error(tmp_path: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(RelayError, match="ConnectError"):
        relay_link(tmp_path, "https://relay.example", httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(RelayError, match="https"):
        relay_link(
            tmp_path, "http://relay.example", registrar([], {})
        )  # the secret would cross the net in clear


def settings(tmp_path: Path, **extra: object) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        llm_fast_models=[],
        llm_quality_models=[],
        dashboard=False,
        **extra,  # type: ignore[arg-type]
    )


def saved_link(tmp_path: Path) -> None:
    (tmp_path / LINK_FILE).write_text(
        json.dumps({"url": "https://relay.example", "secret": "s3cret", "bot": "SarthiJobsBot"})
    )


async def close(sarthi: app_module.App) -> None:
    await sarthi.bot.api.aclose()
    for client in sarthi.ai_clients:
        await client.aclose()
    sarthi.conn.close()


async def test_no_bot_at_all_is_a_setup_error(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="SARTHI_RELAY_URL"):
        build(settings(tmp_path))


async def test_an_existing_own_bot_user_keeps_their_pairing_on_upgrade(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sarthi.db")
    store = ChatStore(conn)
    store.set_state(OWNER_KEY, "111")  # paired before modes were recorded
    store.set_state(OFFSET_KEY, "987654321")
    conn.close()
    sarthi = build(settings(tmp_path, telegram_bot_token=SecretStr("1:x")))
    try:
        assert sarthi.bot.chats.get_state(OWNER_KEY) == "111"
        assert sarthi.bot.chats.get_state(OFFSET_KEY) == "987654321"
        assert sarthi.bot.chats.get_state(MODE_KEY) == "own"
        assert sarthi.relay is None
    finally:
        await close(sarthi)


async def test_switching_to_the_shared_bot_starts_its_chat_link_afresh(tmp_path: Path) -> None:
    sarthi = build(settings(tmp_path, telegram_bot_token=SecretStr("1:x")))
    sarthi.bot.chats.set_state(OWNER_KEY, "111")
    sarthi.bot.chats.set_state(OFFSET_KEY, "987654321")  # the own bot's numbering: meaningless to the relay
    await close(sarthi)
    saved_link(tmp_path)
    sarthi = build(settings(tmp_path, relay_url="https://relay.example"))
    try:
        assert sarthi.relay is not None and sarthi.relay.bot == "SarthiJobsBot"
        assert sarthi.bot.chats.get_state(OFFSET_KEY) is None
        assert sarthi.bot.chats.get_state(OWNER_KEY) is None  # the relay has no chat for it yet
        assert sarthi.bot.chats.get_state(MODE_KEY) == "relay:https://relay.example"
        assert sarthi.bot.api._base == "https://relay.example"
    finally:
        await close(sarthi)


async def test_an_own_token_wins_over_the_shared_bot(tmp_path: Path) -> None:
    sarthi = build(settings(tmp_path, telegram_bot_token=SecretStr("1:x"), relay_url="https://relay.example"))
    try:
        assert sarthi.relay is None
    finally:
        await close(sarthi)


class FakeApi:
    def __init__(self, fail_first: bool = False) -> None:
        self.hashes: list[str] = []
        self.fail_first = fail_first

    async def set_pairing_code(self, value: str) -> None:
        if self.fail_first:
            self.fail_first = False
            from sarthi.queue.tasks import RetryableError  # noqa: PLC0415

            raise RetryableError("relay down")
        self.hashes.append(value)

    async def aclose(self) -> None:
        return None


async def test_the_link_is_shown_until_the_user_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved_link(tmp_path)
    sarthi = build(settings(tmp_path, relay_url="https://relay.example"))
    monkeypatch.setattr(app_module, "PAIR_CHECK_S", 0.01)
    monkeypatch.setattr(app_module, "PAIR_RETRY_S", 0.01)
    fake = FakeApi(fail_first=True)
    real_api, sarthi.bot.api = sarthi.bot.api, fake  # type: ignore[assignment]
    try:
        offering = asyncio.create_task(sarthi.offer_pairing())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if sarthi.bot.chats.get_state(PAIR_LINK_KEY):
                break
        link = sarthi.bot.chats.get_state(PAIR_LINK_KEY)
        assert link is not None and link.startswith("https://t.me/SarthiJobsBot?start=")
        code = link.rsplit("=", 1)[1]
        assert fake.hashes == [code_hash(code)]  # the relay knows the hash, never the code
        assert redeem(sarthi.bot.chats, code, 111, sarthi.clock())  # the user opened it
        await asyncio.wait_for(offering, 2)
        assert sarthi.bot.chats.get_state(PAIR_LINK_KEY) is None
    finally:
        sarthi.bot.api = real_api
        await close(sarthi)


async def test_no_link_is_offered_with_an_own_bot(tmp_path: Path) -> None:
    sarthi = build(settings(tmp_path, telegram_bot_token=SecretStr("1:x")))
    try:
        await asyncio.wait_for(sarthi.offer_pairing(), 1)
        assert sarthi.bot.chats.get_state(PAIR_LINK_KEY) is None
    finally:
        await close(sarthi)


def test_a_blank_secret_line_means_not_set(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "SARTHI_TELEGRAM_BOT_TOKEN=\nSARTHI_GROQ_API_KEY=   \nSARTHI_RELAY_URL=https://relay.example\n"
    )
    loaded = Settings(_env_file=env, data_dir=tmp_path)
    assert loaded.telegram_bot_token is None  # so the shared bot is used, not an empty token
    assert loaded.groq_api_key is None
    assert loaded.relay_url == "https://relay.example"
