"""The few Telegram Bot API methods Sarthi needs, over plain httpx.

The bot token is part of every Telegram URL, so it must never reach a log line or error message: httpx's own
request logging is silenced, and errors here mention only the method name and status.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

import httpx
from pydantic import SecretStr

from sarthi.queue.tasks import PermanentError, RetryableError

logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the full URL, i.e. the token
logging.getLogger("httpcore").setLevel(logging.WARNING)

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_S = 30
CONFLICT_WAIT_S = 15.0
_HTTP_TIMEOUT_S = POLL_TIMEOUT_S + 15
_CONFLICT = 409  # another program is already reading this bot's updates
_TOO_MANY_REQUESTS = 429
_SERVER_ERROR = 500
_NOT_MODIFIED = "message is not modified"

Markup = dict[str, list[list[dict[str, str]]]]


def buttons(*rows: list[tuple[str, str]]) -> Markup:
    """Inline keyboard from rows of ``(label, callback_data)``."""
    return {
        "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]
    }


class TelegramApi:
    """A Telegram bot, identified by its token."""

    def __init__(
        self, token: SecretStr, http: httpx.AsyncClient | None = None, base_url: str = API_BASE
    ) -> None:
        """Talk to Telegram as the bot owning ``token``."""
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
        self._base = base_url.rstrip("/")

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        await self._http.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base}/bot{self._token.get_secret_value()}/{method}"
        try:
            response = await self._http.post(url, json=payload)
        except httpx.HTTPError as error:
            raise RetryableError(f"telegram {method}: {type(error).__name__}") from None
        return self._result(method, response)

    @staticmethod
    def _result(method: str, response: httpx.Response) -> Any:
        body: dict[str, Any] = {}
        with contextlib.suppress(ValueError):  # not JSON: handled below as a failed call
            body = response.json()
        if response.status_code == _CONFLICT:
            raise RetryableError(
                "telegram: another Sarthi is already running with this bot; stop it (Ctrl+C) to continue",
                retry_after=CONFLICT_WAIT_S,
                status=_CONFLICT,
            )
        if response.status_code == _TOO_MANY_REQUESTS:
            wait = float((body.get("parameters") or {}).get("retry_after") or 30)
            raise RetryableError(
                f"telegram {method}: rate limited", retry_after=wait, status=_TOO_MANY_REQUESTS
            )
        if response.status_code >= _SERVER_ERROR:
            raise RetryableError(
                f"telegram {method}: HTTP {response.status_code}", status=response.status_code
            )
        if not body.get("ok"):
            description = str(body.get("description") or f"HTTP {response.status_code}")
            raise PermanentError(f"telegram {method}: {description[:200]}")
        return body.get("result")

    async def _upload(self, method: str, data: dict[str, str], files: dict[str, Any]) -> Any:
        url = f"{self._base}/bot{self._token.get_secret_value()}/{method}"
        try:
            response = await self._http.post(url, data=data, files=files)
        except httpx.HTTPError as error:
            raise RetryableError(f"telegram {method}: {type(error).__name__}") from None
        return self._result(method, response)

    async def get_updates(self, offset: int | None, timeout: int = POLL_TIMEOUT_S) -> list[dict[str, Any]]:
        """New messages and button taps (long polling)."""
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload)
        return list(result or [])

    async def send_message(self, chat_id: int, text: str, markup: Markup | None = None) -> int:
        """Send an HTML message; returns its message id."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if markup is not None:
            payload["reply_markup"] = markup
        result = await self._call("sendMessage", payload)
        return int(result["message_id"])

    async def edit_buttons(self, chat_id: int, message_id: int, markup: Markup | None) -> None:
        """Replace (or remove) a message's buttons. Re-sending the same buttons is not an error."""
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        payload["reply_markup"] = markup or {"inline_keyboard": []}
        try:
            await self._call("editMessageReplyMarkup", payload)
        except PermanentError as error:
            if _NOT_MODIFIED not in str(error):
                raise

    async def download(self, file_id: str, max_bytes: int) -> bytes:
        """The bytes of a file the user sent; ``PermanentError`` when it is larger than ``max_bytes``."""
        info = await self._call("getFile", {"file_id": file_id})
        if int(info.get("file_size") or 0) > max_bytes or "file_path" not in info:
            raise PermanentError("telegram getFile: file too large or unavailable")
        url = f"{self._base}/file/bot{self._token.get_secret_value()}/{info['file_path']}"
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as error:
            raise RetryableError(f"telegram download: {type(error).__name__}") from None
        if response.status_code != httpx.codes.OK or len(response.content) > max_bytes:
            raise PermanentError(f"telegram download: HTTP {response.status_code} or file too large")
        return response.content

    async def send_document(
        self, chat_id: int, filename: str, content: bytes, caption: str = "", mime: str = "text/csv"
    ) -> int:
        """Send a file (HTML caption); returns the message id."""
        data = {"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML"}
        result = await self._upload("sendDocument", data, {"document": (filename, content, mime)})
        return int(result["message_id"])

    async def set_commands(self, commands: tuple[tuple[str, str], ...]) -> None:
        """The command menu Telegram shows when the user types "/" (name without the slash, description)."""
        payload = {"commands": [{"command": name, "description": about[:256]} for name, about in commands]}
        await self._call("setMyCommands", payload)

    async def send_photo(
        self, chat_id: int, photo: bytes, caption: str = "", markup: Markup | None = None
    ) -> int:
        """Send a picture (PNG/JPEG) with an HTML caption and optional buttons; returns the message id."""
        data = {"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML"}
        if markup is not None:
            data["reply_markup"] = json.dumps(markup)
        result = await self._upload("sendPhoto", data, {"photo": ("form.png", photo, "image/png")})
        return int(result["message_id"])

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Acknowledge a button tap (stops the spinner), with an optional short toast."""
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})
