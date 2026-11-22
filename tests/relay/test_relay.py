"""The shared-bot relay, end to end: Sarthi's own TelegramApi -> relay -> a fake Telegram.

Every Sarthi shares one bot, so the rules that keep users apart are tested hardest: a Sarthi reads, writes,
downloads and answers only within the one chat that opened its own pairing link.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sarthi.chat.pairing import code_hash as sarthi_code_hash
from sarthi.chat.telegram.api import TelegramApi
from sarthi.queue.tasks import PermanentError, RetryableError
from sarthi.relay import server
from sarthi.relay.server import LINK_EXPIRED, UNPAIRED_HELP, Relay, code_hash, create_relay_app
from sarthi.relay.store import RelayStore

TOKEN = "123:shared-bot-token"  # noqa: S105  (a made-up token for the fake Telegram)


@dataclass
class FakeTelegram:
    """Records what the relay sends to Telegram and answers like it."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    next_id: int = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert TOKEN in request.url.path  # only the relay knows the real token
        if request.url.path.startswith("/file/"):
            return httpx.Response(200, content=b"%PDF resume bytes")
        method = request.url.path.rsplit("/", 1)[1]
        if request.headers.get("content-type", "").startswith("multipart/"):
            body = {"multipart": request.content.decode("latin-1")}
        else:
            body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        self.next_id += 1
        results: dict[str, Any] = {
            "getMe": {"username": "SarthiJobsBot"},
            "getFile": {"file_id": body.get("file_id"), "file_path": "documents/file_7.pdf", "file_size": 17},
        }
        return httpx.Response(
            200, json={"ok": True, "result": results.get(method, {"message_id": self.next_id})}
        )

    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [body for name, body in self.calls if name == method]


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class World:
    relay: Relay
    telegram: FakeTelegram
    clock: Clock
    http: httpx.AsyncClient

    async def sarthi(self, address: str = "10.0.0.1") -> TelegramApi:
        """A newly registered Sarthi, talking to the relay with its secret."""
        response = await self.http.post("/v1/register", headers={"x-forwarded-for": address})
        secret = response.json()["result"]["secret"]
        return TelegramApi(SecretStr(secret), http=self.http, base_url="http://relay")

    async def user_sends(self, chat_id: int, text: str = "", **extra: Any) -> None:
        message = {"message_id": 1, "chat": {"id": chat_id, "type": "private"}, "text": text, **extra}
        await self.relay.route({"update_id": 555, "message": message})

    async def pair(self, api: TelegramApi, chat_id: int, code: str = "C0FFEE") -> None:
        await api.set_pairing_code(sarthi_code_hash(code))
        await self.user_sends(chat_id, f"/start {code}")


@pytest.fixture
async def world() -> AsyncIterator[World]:
    telegram, clock = FakeTelegram(), Clock()
    relay = Relay(
        RelayStore(":memory:", clock=clock),
        SecretStr(TOKEN),
        http=httpx.AsyncClient(transport=httpx.MockTransport(telegram.handler)),
        base_url="https://telegram.test",
        commands=(("today", "Today's matches"),),
        clock=clock,
    )
    await relay.start()
    app = create_relay_app(relay, trust_forwarded=True)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay")
    yield World(relay, telegram, clock, http)
    await http.aclose()
    await relay.aclose()


def test_the_code_hash_matches_sarthis_own() -> None:
    for code in ("A1B2C3", " a1b2c3 ", "ff00"):
        assert code_hash(code) == sarthi_code_hash(code)


async def test_start_sets_the_menu_and_learns_the_bot_name(world: World) -> None:
    assert world.relay.bot_username == "SarthiJobsBot"
    assert world.telegram.sent("setMyCommands") == [
        {"commands": [{"command": "today", "description": "Today's matches"}]}
    ]


async def test_pairing_then_messages_flow_both_ways_within_that_chat(world: World) -> None:
    api = await world.sarthi()
    assert await api.get_updates(None, timeout=0) == []
    await world.pair(api, chat_id=111)

    updates = await api.get_updates(None, timeout=0)
    assert [u["message"]["text"] for u in updates] == ["/start C0FFEE"]  # Sarthi checks the code again itself
    assert updates[0]["update_id"] != 555  # the relay's own numbering for this Sarthi

    await world.user_sends(111, "hello")
    later = await api.get_updates(updates[-1]["update_id"] + 1, timeout=0)
    assert [u["message"]["text"] for u in later] == ["hello"]
    assert await api.get_updates(later[-1]["update_id"] + 1, timeout=0) == []  # handled ones are gone

    await api.send_message(999_999, "a job for you")  # whatever chat it names...
    assert world.telegram.sent()[-1]["chat_id"] == 111  # ...it reaches its own chat only
    assert world.telegram.sent()[-1]["text"] == "a job for you"


async def test_updates_wait_while_the_sarthi_is_offline(world: World) -> None:
    api = await world.sarthi()
    await world.pair(api, 111)
    for text in ("one", "two", "three"):
        await world.user_sends(111, text)
    texts = [u["message"]["text"] for u in await api.get_updates(None, timeout=0)]
    assert texts == ["/start C0FFEE", "one", "two", "three"]


async def test_a_stranger_reaches_no_sarthi_and_is_told_once_how_to_start(world: World) -> None:
    api = await world.sarthi()
    await world.pair(api, 111)
    await api.get_updates(None, timeout=0)
    before = len(world.telegram.sent())
    await world.user_sends(222, "hi")
    await world.user_sends(222, "hello?")
    assert [m["text"] for m in world.telegram.sent()[before:]] == [
        UNPAIRED_HELP
    ]  # once an hour, not each time
    assert all(m["chat_id"] == 222 for m in world.telegram.sent()[before:])
    pending = await api.get_updates(None, timeout=0)
    assert all(u["message"]["chat"]["id"] == 111 for u in pending)


async def test_a_wrong_or_expired_link_pairs_nothing(world: World) -> None:
    api = await world.sarthi()
    await api.set_pairing_code(sarthi_code_hash("RIGHT1"))
    await world.user_sends(333, "/start WRONG1")
    assert world.telegram.sent()[-1] == {"chat_id": 333, "text": LINK_EXPIRED, "parse_mode": "HTML"}
    world.clock.now += server.PAIRING_LIFETIME_S + 1
    await world.user_sends(333, "/start RIGHT1")  # the right code, too late
    assert world.telegram.sent()[-1]["text"] == LINK_EXPIRED
    with pytest.raises(PermanentError, match="not connected"):
        await api.send_message(333, "x")


async def test_a_code_works_once(world: World) -> None:
    first = await world.sarthi()
    await world.pair(first, 111, "ONCE01")
    await world.user_sends(444, "/start ONCE01")  # someone who saw the link afterwards
    assert world.telegram.sent()[-1] == {"chat_id": 444, "text": LINK_EXPIRED, "parse_mode": "HTML"}


async def test_sarthis_cannot_reach_each_others_chats_files_or_buttons(world: World) -> None:
    alice, bob = await world.sarthi("10.0.0.1"), await world.sarthi("10.0.0.2")
    await world.pair(alice, 111, "ALICE1")
    with pytest.raises(PermanentError, match="not connected"):
        await bob.send_message(111, "spam")  # not paired: cannot send at all
    await world.pair(bob, 222, "BOB001")
    await bob.send_message(111, "spam")
    assert world.telegram.sent()[-1]["chat_id"] == 222  # Bob's own chat, never Alice's

    await world.user_sends(111, "", document={"file_id": "ALICE-FILE", "file_size": 17})
    await world.relay.route(
        {
            "update_id": 9,
            "callback_query": {"id": "ALICE-TAP", "data": "apply:1", "message": {"chat": {"id": 111}}},
        }
    )
    with pytest.raises(PermanentError, match="not a file from your chat"):
        await bob.download("ALICE-FILE", 1000)
    with pytest.raises(PermanentError, match="not a button tap"):
        await bob.answer_callback("ALICE-TAP", "gotcha")
    bob_updates = await bob.get_updates(None, timeout=0)
    assert all(u.get("message", {}).get("chat", {}).get("id") == 222 for u in bob_updates)

    assert await alice.download("ALICE-FILE", 1000) == b"%PDF resume bytes"  # her own file works
    await alice.answer_callback("ALICE-TAP")
    assert world.telegram.sent("answerCallbackQuery")[-1]["callback_query_id"] == "ALICE-TAP"


async def test_a_file_path_cannot_be_guessed(world: World) -> None:
    alice = await world.sarthi()
    await world.pair(alice, 111)
    response = await world.http.get(f"/file/bot{alice._token.get_secret_value()}/documents/file_7.pdf")
    assert response.status_code == 403  # never handed out by getFile to this Sarthi


async def test_documents_and_photos_go_to_the_own_chat(world: World) -> None:
    api = await world.sarthi()
    await world.pair(api, 111)
    await api.send_document(999, "Resume.pdf", b"%PDF-1.7 x", caption="Your resume", mime="application/pdf")
    await api.send_photo(999, b"\x89PNG x", caption="The form")
    uploads = [
        body["multipart"] for name, body in world.telegram.calls if name in ("sendDocument", "sendPhoto")
    ]
    assert len(uploads) == 2
    for upload in uploads:
        assert 'name="chat_id"\r\n\r\n111' in upload
        assert "999" not in upload


async def test_moving_to_a_new_computer_releases_the_old_sarthi(world: World) -> None:
    old, new = await world.sarthi("10.0.0.1"), await world.sarthi("10.0.0.2")
    await world.pair(old, 111, "OLD001")
    await world.pair(new, 111, "NEW001")  # the same person, a new install
    await new.send_message(1, "hi from new")
    assert world.telegram.sent()[-1]["chat_id"] == 111
    with pytest.raises(PermanentError, match="not connected"):
        await old.send_message(1, "hi from old")


async def test_sending_is_paced_per_sarthi(world: World) -> None:
    api = await world.sarthi()
    await world.pair(api, 111)
    for _ in range(server.SENDS_PER_MINUTE):
        await api.send_message(1, "x")
    with pytest.raises(RetryableError) as raised:
        await api.send_message(1, "one too many")
    assert raised.value.retry_after == 30
    world.clock.now += 61
    await api.send_message(1, "a minute later")


async def test_sign_ups_are_limited_per_address(world: World) -> None:
    for _ in range(server.REGISTER_PER_HOUR):
        await world.sarthi("10.9.9.9")
    response = await world.http.post("/v1/register", headers={"x-forwarded-for": "10.9.9.9"})
    assert response.status_code == 429
    assert (await world.http.post("/v1/register", headers={"x-forwarded-for": "10.9.9.8"})).status_code == 200


async def test_a_bad_secret_or_another_method_is_refused(world: World) -> None:
    stranger = TelegramApi(SecretStr("made-up"), http=world.http, base_url="http://relay")
    with pytest.raises(PermanentError, match="Unauthorized"):
        await stranger.get_updates(None, timeout=0)
    api = await world.sarthi()
    await world.pair(api, 111)
    for method in ("forwardMessage", "getChat", "banChatMember", "deleteWebhook", "setWebhook", "logOut"):
        response = await world.http.post(f"/bot{api._token.get_secret_value()}/{method}", json={"chat_id": 5})
        assert response.status_code == 404, method
    await api.set_commands((("evil", "Click http://scam.example"),))  # accepted, and ignored
    assert world.telegram.sent("setMyCommands")[-1]["commands"][0]["command"] == "today"


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"'])
async def test_a_malformed_call_is_refused(world: World, body: bytes) -> None:
    api = await world.sarthi()
    response = await world.http.post(f"/bot{api._token.get_secret_value()}/sendMessage", content=body)
    assert response.status_code == 400


@pytest.mark.parametrize("value", ["", "abc", "Z" * 64, "a" * 63, "a" * 65])
async def test_a_pairing_code_hash_must_be_a_hash(world: World, value: str) -> None:
    api = await world.sarthi()
    with pytest.raises(PermanentError, match="sha256"):
        await api.set_pairing_code(value)


async def test_groups_and_updates_without_a_chat_are_ignored(world: World) -> None:
    api = await world.sarthi()
    await world.pair(api, 111)
    handled = await api.get_updates(None, timeout=0)
    before = len(world.telegram.calls)
    await world.relay.route({"update_id": 1, "message": {"chat": {"id": 111, "type": "group"}, "text": "hi"}})
    await world.relay.route({"update_id": 2, "edited_message": {"chat": {"id": 111}}})
    await world.relay.route({"update_id": 3})
    assert await api.get_updates(handled[-1]["update_id"] + 1, timeout=0) == []
    assert len(world.telegram.calls) == before


async def test_old_updates_and_unpaired_sarthis_are_forgotten(world: World) -> None:
    api = await world.sarthi()
    await world.pair(api, 111)
    await world.sarthi("10.0.0.5")  # never paired
    world.clock.now += 8 * 86400
    world.relay.store.clean()
    assert await api.get_updates(None, timeout=0) == []
    count = world.relay.store.conn.execute("SELECT COUNT(*) FROM instance").fetchone()[0]
    assert count == 1  # the paired one stays


async def test_a_long_poll_wakes_when_an_update_arrives(world: World) -> None:
    import asyncio  # noqa: PLC0415

    api = await world.sarthi()
    await world.pair(api, 111)
    first = await api.get_updates(None, timeout=0)
    waiting = asyncio.create_task(api.get_updates(first[-1]["update_id"] + 1, timeout=20))
    await asyncio.sleep(0.05)
    await world.user_sends(111, "now")
    got = await asyncio.wait_for(waiting, 2)
    assert [u["message"]["text"] for u in got] == ["now"]
