"""The user's own bot gets Sarthi's logo and description, once, without overwriting their own."""

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sarthi.chat.telegram.api import TelegramApi
from sarthi.chat.telegram.brand import AVATAR, BRANDED_KEY, SHORT_DESCRIPTION, brand_bot
from sarthi.queue.tasks import PermanentError, RetryableError


class State:
    def __init__(self) -> None:
        self.values: dict[str, str | None] = {}

    def get_state(self, key: str) -> str | None:
        return self.values.get(key)

    def set_state(self, key: str, value: str | None) -> None:
        self.values[key] = value


class Api:
    def __init__(self, me: dict[str, Any], photo: bool = False, about: str = "") -> None:
        self._me, self.photo, self.about = me, photo, about
        self.calls: list[str] = []
        self.fail: Exception | None = None

    async def me(self) -> dict[str, Any]:
        if self.fail:
            raise self.fail
        return self._me

    async def has_profile_photo(self, user_id: int) -> bool:
        self.calls.append(f"photo?{user_id}")
        return self.photo

    async def set_profile_photo(self, jpeg: bytes) -> None:
        assert jpeg[:3] == b"\xff\xd8\xff"  # a JPEG
        self.calls.append("set_photo")

    async def description(self) -> str:
        return self.about

    async def set_description(self, description: str, short: str) -> None:
        assert len(description) <= 512 and len(short) <= 120
        self.calls.append("set_description")


async def test_a_new_bot_gets_logo_and_description_once() -> None:
    api, state = Api({"id": 42, "username": "sarthi_x_bot"}), State()
    assert await brand_bot(api, state)
    assert api.calls == ["photo?42", "set_photo", "set_description"]
    assert state.values[BRANDED_KEY] == "42"
    api.calls.clear()
    assert await brand_bot(api, state)  # next start: nothing more
    assert api.calls == []


async def test_a_picture_or_text_the_user_set_is_kept() -> None:
    api = Api({"id": 7}, photo=True, about="My own words")
    assert await brand_bot(api, State())
    assert api.calls == ["photo?7"]


async def test_a_new_bot_after_switching_is_branded_too() -> None:
    state = State()
    state.set_state(BRANDED_KEY, "1")
    api = Api({"id": 2})
    assert await brand_bot(api, state)
    assert "set_photo" in api.calls


async def test_the_shared_relay_bot_is_never_touched() -> None:
    api = Api({"username": "relay_bot"})
    assert not await brand_bot(api, State())
    assert api.calls == []


@pytest.mark.parametrize("error", [RetryableError("offline"), PermanentError("Bad Request")])
async def test_a_failure_is_retried_at_the_next_start(error: Exception) -> None:
    api, state = Api({"id": 3}), State()
    api.fail = error
    assert not await brand_bot(api, state)
    assert BRANDED_KEY not in state.values


def test_the_logo_ships_with_the_package() -> None:
    data = AVATAR.read_bytes()
    assert data[:3] == b"\xff\xd8\xff"
    assert len(data) < 5_000_000  # Telegram's photo limit is far above this
    assert len(SHORT_DESCRIPTION) <= 120


async def test_api_sends_the_photo_as_an_attached_static_photo() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/getUserProfilePhotos"):
            return httpx.Response(200, json={"ok": True, "result": {"total_count": 0, "photos": []}})
        return httpx.Response(200, json={"ok": True, "result": True})

    client = TelegramApi(SecretStr("1:abc"), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert not await client.has_profile_photo(5)
    await client.set_profile_photo(b"\xff\xd8\xffjpeg")
    body = seen[1].content
    assert seen[1].url.path.endswith("/setMyProfilePhoto")
    assert b'name="avatar"' in body
    assert json.dumps({"type": "static", "photo": "attach://avatar"}).encode() in body
    await client.set_description("long", "short")
    assert [r.url.path.rsplit("/", 1)[1] for r in seen[2:]] == ["setMyDescription", "setMyShortDescription"]
