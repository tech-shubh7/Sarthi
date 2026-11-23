"""One-tap Telegram setup: the user gets a bot of their own through the Sarthi connector (managed bots)."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sarthi import app as app_module
from sarthi.app import SetupError, build
from sarthi.chat.pairing import OWNER_KEY, redeem
from sarthi.chat.telegram.bot import OFFSET_KEY
from sarthi.chat.telegram.connect import (
    BOT_FILE,
    STEP_KEY,
    ConnectError,
    Connector,
    ManagedBot,
    save_bot,
    saved_bot,
)
from sarthi.chat.telegram.link import MODE_KEY, PAIR_LINK_KEY
from sarthi.config import Settings

TOKEN = "7123456789:TEST_fictional_token_not_real_abcdefgh"  # noqa: S105  (fictional)
URL = "http://127.0.0.1:8799"


class FakeConnector:
    """The connector's two calls, scripted: answers "pending" until ``ready_after`` reads."""

    def __init__(self, ready_after: int = 2, token: str = TOKEN) -> None:
        self.reads = 0
        self.ready_after = ready_after
        self.token = token
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "id": "s" * 24,
                    "secret": "x" * 40,
                    "start_link": "https://t.me/SarthiiAiBot?start=c_abc",
                },
            )
        if request.headers.get("authorization") != "Bearer " + "x" * 40:
            return httpx.Response(404, json={"ok": False, "error": "unknown or expired session"})
        self.reads += 1
        if self.reads < self.ready_after:
            return httpx.Response(200, json={"ok": True, "status": "paired"})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "status": "ready",
                "token": self.token,
                "bot_username": "aarav_jobs_bot",
                "owner_id": 4242,
            },
        )


def connector(fake: FakeConnector) -> Connector:
    return Connector(URL, httpx.AsyncClient(transport=httpx.MockTransport(fake)))


async def test_a_session_link_then_the_bot_once_ready() -> None:
    fake = FakeConnector(ready_after=2)
    c = connector(fake)
    session = await c.session()
    assert session.link == "https://t.me/SarthiiAiBot?start=c_abc"
    assert await c.collect(session) is None  # not made yet
    bot = await c.collect(session)
    assert bot is not None
    assert (bot.token.get_secret_value(), bot.username, bot.owner_id) == (TOKEN, "aarav_jobs_bot", 4242)
    assert bot.start_link("CODE") == "https://t.me/aarav_jobs_bot?start=CODE"
    await c.aclose()


@pytest.mark.parametrize("token", ["", "not-a-token", "123:short", "123:" + "x" * 40 + " ; rm -rf /"])
async def test_anything_that_is_not_a_bot_token_is_refused(token: str) -> None:
    c = connector(FakeConnector(ready_after=1, token=token))
    with pytest.raises(ConnectError, match=r"not a bot token|incomplete"):
        await c.collect(await c.session())
    await c.aclose()


async def test_errors_from_the_connector_are_reported_not_raised_raw() -> None:
    down = Connector(
        URL, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502, text="bad")))
    )
    with pytest.raises(ConnectError, match="could not reach"):
        await down.session()
    refused = Connector(
        URL,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(429, json={"ok": False, "error": "too many setups"})
            )
        ),
    )
    with pytest.raises(ConnectError, match="too many setups"):
        await refused.session()
    with pytest.raises(ConnectError, match="https"):
        Connector("http://connector.example")  # never in clear text over the internet


def test_the_bot_token_is_kept_privately_and_read_back(tmp_path: Path) -> None:
    bot = ManagedBot(SecretStr(TOKEN), "aarav_jobs_bot", 4242)
    save_bot(tmp_path, bot)
    assert stat.S_IMODE((tmp_path / BOT_FILE).stat().st_mode) == 0o600
    assert saved_bot(tmp_path) == bot
    (tmp_path / BOT_FILE).write_text(json.dumps({"token": "junk", "username": "a b", "owner_id": 1}))
    assert saved_bot(tmp_path) is None  # a broken file means "set up again", never a crash


def settings(tmp_path: Path, **values: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "data_dir": tmp_path,
        "llm_fast_models": [],
        "llm_quality_models": [],
    }
    return Settings(**(base | values))


async def close(sarthi: Any) -> None:
    await sarthi.bot.api.aclose()
    for client in sarthi.ai_clients:
        await client.aclose()
    if sarthi.connector is not None:
        await sarthi.connector.aclose()
    sarthi.conn.close()


async def test_without_any_bot_sarthi_starts_one_tap_setup(tmp_path: Path) -> None:
    sarthi = build(settings(tmp_path, telegram_connect_url=URL))
    try:
        assert sarthi.connector is not None
        assert sarthi.bot.chats.get_state(MODE_KEY) == "connect"
    finally:
        await close(sarthi)
    with pytest.raises(SetupError, match="https"):
        build(settings(tmp_path, telegram_connect_url="http://connector.example"))


async def test_one_tap_setup_switches_to_the_users_own_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "CONNECT_CHECK_S", 0.01)
    sarthi = build(settings(tmp_path, telegram_connect_url=URL))
    sarthi.bot.chats.set_state(OFFSET_KEY, "55")  # left over: meaningless to the new bot
    fake = FakeConnector(ready_after=3)
    try:
        task = asyncio.create_task(sarthi.connect_bot(connector(fake)))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if sarthi.bot.chats.get_state(PAIR_LINK_KEY):
                break
        # Step 1 on the dashboard: the connector's link.
        assert sarthi.bot.chats.get_state(PAIR_LINK_KEY) == "https://t.me/SarthiiAiBot?start=c_abc"
        assert sarthi.bot.chats.get_state(STEP_KEY) == "create"
        await asyncio.wait_for(task, 3)
        assert sarthi.bot.api._token.get_secret_value() == TOKEN
        assert sarthi.bot.api._base == "https://api.telegram.org"
        assert saved_bot(tmp_path) is not None
        assert sarthi.bot.chats.get_state(MODE_KEY) == "managed:aarav_jobs_bot"
        assert sarthi.bot.chats.get_state(STEP_KEY) == "start2"
        assert sarthi.bot.chats.get_state(OFFSET_KEY) is None
        assert sarthi.connector is None
    finally:
        await close(sarthi)
    # Next start: the saved bot is used straight away, no setup.
    again = build(settings(tmp_path, telegram_connect_url=URL))
    try:
        assert again.connector is None
        assert again.bot.api._token.get_secret_value() == TOKEN
        assert again.bot.chats.get_state(MODE_KEY) == "managed:aarav_jobs_bot"
    finally:
        await close(again)


class NamedApi:
    def __init__(self) -> None:
        self.asked = 0

    async def username(self) -> str:
        self.asked += 1
        return "aarav_jobs_bot"

    async def aclose(self) -> None:
        return None


async def test_a_bot_of_ones_own_shows_its_start_link_until_the_user_taps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "PAIR_CHECK_S", 0.01)
    save_bot(tmp_path, ManagedBot(SecretStr(TOKEN), "aarav_jobs_bot", 4242))
    sarthi = build(settings(tmp_path))
    real_api, sarthi.bot.api = sarthi.bot.api, NamedApi()  # type: ignore[assignment]
    try:
        offering = asyncio.create_task(sarthi.offer_pairing())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if sarthi.bot.chats.get_state(PAIR_LINK_KEY):
                break
        link = sarthi.bot.chats.get_state(PAIR_LINK_KEY)
        assert link is not None and link.startswith("https://t.me/aarav_jobs_bot?start=")
        assert redeem(sarthi.bot.chats, link.rsplit("=", 1)[1], 4242, sarthi.clock())  # the user tapped Start
        await asyncio.wait_for(offering, 2)
        assert sarthi.bot.chats.get_state(PAIR_LINK_KEY) is None
        assert sarthi.bot.chats.get_state(OWNER_KEY) == "4242"
    finally:
        sarthi.bot.api = real_api
        await close(sarthi)
