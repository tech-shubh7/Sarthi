import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sarthi.chat.telegram.api import TelegramApi, buttons
from sarthi.queue.tasks import PermanentError, RetryableError

TOKEN = "123456789:" + "AA" + "z" * 33  # fake, assembled at runtime


def api(handler: Any) -> TelegramApi:
    return TelegramApi(SecretStr(TOKEN), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_send_message_posts_html_with_buttons() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    message_id = await api(handler).send_message(7, "<b>hi</b>", buttons([("Apply", "apply:x")]))
    assert message_id == 42
    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(seen[0].content)
    assert body["parse_mode"] == "HTML"
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "Apply", "callback_data": "apply:x"}]]}
    assert body["link_preview_options"] == {"is_disabled": True}


async def test_errors_never_contain_the_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def bad_token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    with pytest.raises(PermanentError) as caught:
        await api(bad_token).get_updates(None)
    assert TOKEN not in str(caught.value)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(RetryableError) as down:
        await api(offline).send_message(1, "x")
    assert TOKEN not in str(down.value)
    assert down.value.__cause__ is None
    assert TOKEN not in caplog.text


async def test_rate_limit_and_server_errors_are_retryable() -> None:
    limited = api(lambda r: httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 12}}))
    with pytest.raises(RetryableError) as caught:
        await limited.send_message(1, "x")
    assert caught.value.retry_after == 12
    with pytest.raises(RetryableError):
        await api(lambda r: httpx.Response(502, text="bad gateway")).send_message(1, "x")


async def test_unchanged_buttons_are_not_an_error() -> None:
    same = api(
        lambda r: httpx.Response(
            400, json={"ok": False, "description": "Bad Request: message is not modified"}
        )
    )
    await same.edit_buttons(1, 2, None)
    gone = api(
        lambda r: httpx.Response(
            400, json={"ok": False, "description": "Bad Request: message to edit not found"}
        )
    )
    with pytest.raises(PermanentError):
        await gone.edit_buttons(1, 2, None)


async def test_get_updates_and_callbacks() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5}]})
        return httpx.Response(200, json={"ok": True, "result": True})

    client = api(handler)
    assert await client.get_updates(9) == [{"update_id": 5}]
    await client.answer_callback("cb", "x" * 300)
    assert bodies[0]["offset"] == 9
    assert bodies[0]["allowed_updates"] == ["message", "callback_query"]
    assert len(bodies[1]["text"]) == 200
    await client.aclose()


async def test_a_second_running_sarthi_waits_instead_of_stopping() -> None:
    conflict = api(
        lambda r: httpx.Response(
            409, json={"ok": False, "description": "Conflict: terminated by other getUpdates"}
        )
    )
    with pytest.raises(RetryableError, match="another Sarthi is already running") as caught:
        await conflict.get_updates(None)
    assert caught.value.retry_after == 15.0


async def test_set_commands_and_send_document() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 9} if "Document" in request.url.path else True}
        )

    client = api(handler)
    await client.set_commands((("today", "Best matches"),))
    assert seen[0].url.path.endswith("/setMyCommands")
    assert json.loads(seen[0].content) == {"commands": [{"command": "today", "description": "Best matches"}]}
    assert await client.send_document(7, "matches.csv", b"a,b\r\n", caption="All") == 9
    upload = seen[1].content
    assert b'filename="matches.csv"' in upload
    assert b"a,b" in upload
