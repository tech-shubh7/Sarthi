"""The relay server: holds the one shared bot token and gives each Sarthi its own chat, and only that.

Each Sarthi talks to the relay exactly as it talks to api.telegram.org, with its own secret where a bot token
would be (``https://relay/bot<secret>/<method>``), so the bot code is the same in both modes. The relay:

* gives a Sarthi only the updates from the one chat paired with it, and holds them while it is offline;
* sends a Sarthi's messages to that chat only, whatever ``chat_id`` it names;
* lets a Sarthi download only the files, and answer only the button taps, that came from its chat;
* paces every Sarthi (per minute and per day) and all of them together (Telegram's own limit), so one
  misbehaving install cannot get the shared bot blocked.

Pairing: a Sarthi registers the hash of its one-time code (``setPairingCode``); its user opens
``t.me/<bot>?start=<code>``; the relay binds that chat to the Sarthi whose code matches and forwards the
``/start``, which the Sarthi checks once more itself. Message text passes through but is never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import SecretStr

from sarthi.relay.store import RelayStore, secret_hash

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the full URL, i.e. the token

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_S = 30
MAX_WAIT_S = 50  # the longest a Sarthi's getUpdates is held open
MAX_BODY_BYTES = 12 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024  # Telegram's own download limit for bots
PAIRING_LIFETIME_S = 15 * 60
SENDS_PER_MINUTE = 40
SENDS_PER_DAY = 2000
REGISTER_PER_HOUR = 10  # new Sarthis per address and hour
GLOBAL_SENDS_PER_SECOND = 25  # Telegram allows about 30 per second for one bot
HELP_EVERY_S = 3600  # an unpaired chat is told how to pair at most once an hour
CLEAN_EVERY_S = 3600
_CODE_HASH = re.compile(r"^[0-9a-f]{64}$")
_SEND = frozenset({"sendMessage", "editMessageReplyMarkup", "sendDocument", "sendPhoto"})

UNPAIRED_HELP = (
    "👋 This is the Sarthi bot. To use it, run Sarthi on your computer (<code>docker compose up -d</code>), "
    "then open the link it shows at http://localhost:8765. That link connects this chat to your Sarthi."
)
LINK_EXPIRED = (
    "That link has expired or was already used. Open the newest link shown at http://localhost:8765 "
    "(a new one appears every 15 minutes until you connect)."
)


def code_hash(code: str) -> str:
    """The hash of a pairing code, as Sarthi computes it (``sarthi.chat.pairing``)."""
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def _error(status: int, description: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error_code": status, "description": description}, status_code=status)


def _ok(result: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "result": result})


class _Window:
    """Events in the last ``span`` seconds, to cap how many are allowed."""

    def __init__(self, span: float, limit: int) -> None:
        self.span, self.limit = span, limit
        self.times: dict[object, deque[float]] = defaultdict(deque)

    def allow(self, key: object, now: float) -> bool:
        times = self.times[key]
        while times and times[0] <= now - self.span:
            times.popleft()
        if len(times) >= self.limit:
            return False
        times.append(now)
        return True


class Relay:
    """The shared bot and the Sarthis behind it."""

    def __init__(  # noqa: PLR0913  (explicit, injectable dependencies)
        self,
        store: RelayStore,
        token: SecretStr,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = API_BASE,
        commands: tuple[tuple[str, str], ...] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Serve Sarthis with the bot owning ``token``; ``commands`` is the menu set for every chat."""
        self.store = store
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=POLL_TIMEOUT_S + 15)
        self._base = base_url.rstrip("/")
        self._commands = commands
        self._clock = clock
        self.bot_username = ""
        self._ready: dict[int, asyncio.Event] = defaultdict(asyncio.Event)
        self._per_minute = _Window(60, SENDS_PER_MINUTE)
        self._per_day = _Window(86400, SENDS_PER_DAY)
        self._registrations = _Window(3600, REGISTER_PER_HOUR)
        self._helped: dict[int, float] = {}
        self._pace = asyncio.Lock()
        self._last_send = 0.0
        self._stopping = asyncio.Event()

    # ---- the real bot ----

    def _url(self, method: str) -> str:
        return f"{self._base}/bot{self._token.get_secret_value()}/{method}"

    async def telegram(
        self, method: str, payload: dict[str, Any] | None = None, *, files: dict[str, Any] | None = None
    ) -> httpx.Response:
        """Call the real Bot API (sends paced for the whole bot)."""
        if method in _SEND or method == "answerCallbackQuery":
            async with self._pace:
                wait = self._last_send + 1 / GLOBAL_SENDS_PER_SECOND - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_send = time.monotonic()
        if files is not None:
            return await self._http.post(self._url(method), data=payload or {}, files=files)
        return await self._http.post(self._url(method), json=payload or {})

    async def start(self) -> None:
        """Learn the bot's name and set its command menu."""
        me = (await self.telegram("getMe")).json()
        self.bot_username = str((me.get("result") or {}).get("username") or "")
        if self._commands:
            menu = [{"command": name, "description": about[:256]} for name, about in self._commands]
            await self.telegram("setMyCommands", {"commands": menu})

    async def poll(self) -> None:
        """Receive the bot's updates and hand each to the Sarthi paired with its chat, until ``stop()``."""
        offset = int(self.store.get_state("offset") or 0) or None
        last_clean = 0.0
        while not self._stopping.is_set():
            if self._clock() - last_clean > CLEAN_EVERY_S:
                self.store.clean()
                last_clean = self._clock()
            payload: dict[str, Any] = {
                "timeout": POLL_TIMEOUT_S,
                "allowed_updates": ["message", "callback_query"],
            }
            if offset is not None:
                payload["offset"] = offset
            try:
                body = (await self.telegram("getUpdates", payload)).json()
            except (httpx.HTTPError, ValueError) as error:
                log.warning("relay: getUpdates failed (%s); retrying", type(error).__name__)
                await asyncio.sleep(5)
                continue
            if not body.get("ok"):
                log.warning("relay: getUpdates refused: %s", str(body.get("description"))[:200])
                await asyncio.sleep(15)
                continue
            for update in body.get("result") or []:
                offset = int(update["update_id"]) + 1
                try:
                    await self.route(update)
                except Exception:  # one bad update must never stop the bot for everyone
                    log.exception("relay: could not route an update")
                self.store.set_state("offset", str(offset))

    def stop(self) -> None:
        """Stop polling after the current request."""
        self._stopping.set()

    async def route(self, update: dict[str, Any]) -> None:
        """Hand one update to its chat's Sarthi; pair on ``/start <code>``; tell strangers how to start."""
        message = update.get("message") or {}
        callback = update.get("callback_query") or {}
        chat = (message.get("chat") or (callback.get("message") or {}).get("chat") or {}).get("id")
        if chat is None:
            return
        chat_id = int(chat)
        if message and (message.get("chat") or {}).get("type", "private") != "private":
            return  # groups are not supported: a group's members are not one person
        text = str(message.get("text") or "").strip()
        command, _, argument = text.partition(" ")
        if command.split("@", 1)[0].lower() == "/start" and argument.strip():
            paired = self.store.pair(code_hash(argument), chat_id)
            if paired is None and self.store.instance_of_chat(chat_id) is None:
                await self._tell(chat_id, LINK_EXPIRED)
                return
        instance = self.store.instance_of_chat(chat_id)
        if instance is None:
            if callback:
                await self.telegram("answerCallbackQuery", {"callback_query_id": str(callback.get("id", ""))})
            elif self._clock() - self._helped.get(chat_id, 0.0) > HELP_EVERY_S:
                self._helped[chat_id] = self._clock()
                await self._tell(chat_id, UNPAIRED_HELP)
            return
        for file_id in _file_ids(message):
            self.store.grant("file", file_id, instance)
        if callback.get("id"):
            self.store.grant("callback", str(callback["id"]), instance)
        update = {key: value for key, value in update.items() if key != "update_id"}
        self.store.push(instance, update)
        self._ready[instance].set()

    async def _tell(self, chat_id: int, text: str) -> None:
        with contextlib.suppress(httpx.HTTPError):
            await self.telegram("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

    # ---- the Sarthis ----

    def register(self, address: str) -> JSONResponse:
        """A new Sarthi: its secret (shown once) and the bot's name for the pairing link."""
        if not self._registrations.allow(address, self._clock()):
            return _error(429, "Too Many Requests: retry after 3600")
        secret = secrets.token_urlsafe(32)
        self.store.add_instance(secret)
        return _ok({"secret": secret, "bot": self.bot_username})

    async def call(self, secret: str, method: str, request: Request) -> Response:
        """One Bot API call from a Sarthi, held to its own chat."""
        instance = self.store.instance_for(secret)
        if instance is None:
            return _error(401, "Unauthorized")
        if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
            return _error(413, "Request Entity Too Large")
        iid = int(instance["id"])
        multipart = request.headers.get("content-type", "").startswith("multipart/form-data")
        if multipart:
            form = await request.form()
            payload: dict[str, Any] = {k: v for k, v in form.items() if isinstance(v, str)}
            files = {
                k: (v.filename or "file", await v.read(), v.content_type or "application/octet-stream")
                for k, v in form.items()
                if not isinstance(v, str)
            }
            if sum(len(f[1]) for f in files.values()) > MAX_BODY_BYTES:
                return _error(413, "Request Entity Too Large")
        else:
            try:
                payload = json.loads(await request.body() or b"{}")
            except ValueError:
                return _error(400, "Bad Request: body is not JSON")
            if not isinstance(payload, dict):
                return _error(400, "Bad Request: body is not a JSON object")
            files = {}
        return await self._method(iid, method, payload, files or None)

    async def _method(  # noqa: PLR0911  (one answer per method)
        self, iid: int, method: str, payload: dict[str, Any], files: dict[str, Any] | None
    ) -> Response:
        if method == "getUpdates":
            return await self._updates(iid, payload)
        if method == "setPairingCode":
            value = str(payload.get("code_hash") or "")
            if not _CODE_HASH.match(value):
                return _error(400, "Bad Request: code_hash must be a sha256 hex digest")
            self.store.set_pairing(iid, value, PAIRING_LIFETIME_S)
            return _ok(True)
        if method in ("setMyCommands", "getMe"):  # the menu is the relay's; a Sarthi may not change it
            return _ok(True if method == "setMyCommands" else {"username": self.bot_username})
        if method == "getFile":
            file_id = str(payload.get("file_id") or "")
            if not self.store.granted("file", file_id, iid):
                return _error(403, "Forbidden: not a file from your chat")
            response = await self.telegram("getFile", {"file_id": file_id})
            body = response.json()
            if body.get("ok") and (path := (body.get("result") or {}).get("file_path")):
                self.store.grant("path", str(path), iid)
            return JSONResponse(body, status_code=response.status_code)
        if method == "answerCallbackQuery":
            if not self.store.granted("callback", str(payload.get("callback_query_id") or ""), iid):
                return _error(403, "Forbidden: not a button tap from your chat")
            answer = {
                "callback_query_id": payload["callback_query_id"],
                "text": str(payload.get("text", ""))[:200],
            }
            return await self._forward("answerCallbackQuery", answer, None)
        if method not in _SEND:
            return _error(404, f"Not Found: {method[:40]} is not available through the Sarthi relay")
        chat_id = self.store.chat_of(iid)
        if chat_id is None:
            return _error(403, "Forbidden: this Sarthi is not connected to a chat yet")
        now = self._clock()
        if not self._per_minute.allow(iid, now) or not self._per_day.allow(iid, now):
            return JSONResponse(
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 30},
                },
                status_code=429,
            )
        payload = {**payload, "chat_id": chat_id if files is None else str(chat_id)}  # its own chat, always
        return await self._forward(method, payload, files)

    async def _forward(self, method: str, payload: dict[str, Any], files: dict[str, Any] | None) -> Response:
        try:
            response = await self.telegram(method, payload, files=files)
        except httpx.HTTPError:
            return _error(502, "Bad Gateway: Telegram could not be reached")
        return Response(response.content, status_code=response.status_code, media_type="application/json")

    async def _updates(self, iid: int, payload: dict[str, Any]) -> Response:
        offset = payload.get("offset")
        offset = int(offset) if isinstance(offset, int | str) and str(offset).lstrip("-").isdigit() else None
        wait = min(max(float(payload.get("timeout") or 0), 0.0), MAX_WAIT_S)
        event = self._ready[iid]
        event.clear()  # before reading, so an update pushed in between still wakes this call
        found = self.store.take(iid, offset)
        if not found and wait > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), wait)
            found = self.store.take(iid, None)
        return _ok(found)

    async def download(self, secret: str, path: str) -> Response:
        """A file its chat sent, by the path ``getFile`` gave this Sarthi."""
        instance = self.store.instance_for(secret)
        if instance is None:
            return _error(401, "Unauthorized")
        if not self.store.granted("path", path, int(instance["id"])):
            return _error(403, "Forbidden: not a file from your chat")
        url = f"{self._base}/file/bot{self._token.get_secret_value()}/{path}"
        try:
            response = await self._http.get(url)
        except httpx.HTTPError:
            return _error(502, "Bad Gateway: Telegram could not be reached")
        if len(response.content) > MAX_FILE_BYTES:
            return _error(413, "file too large")
        return Response(response.content, status_code=response.status_code)

    async def aclose(self) -> None:
        """Close the connection pool to Telegram."""
        await self._http.aclose()


def _file_ids(message: dict[str, Any]) -> list[str]:
    ids = [str((message.get("document") or {}).get("file_id") or "")]
    ids += [str(size.get("file_id") or "") for size in message.get("photo") or []]
    return [i for i in ids if i]


def create_relay_app(relay: Relay, *, trust_forwarded: bool = False) -> FastAPI:
    """The relay's web app. ``trust_forwarded``: behind a proxy, count registrations per X-Forwarded-For."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def address(request: Request) -> str:
        if trust_forwarded and (forwarded := request.headers.get("x-forwarded-for")):
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/register")
    async def register(request: Request) -> Response:
        return relay.register(address(request))

    @app.post("/bot{secret}/{method}")
    async def call(secret: str, method: str, request: Request) -> Response:
        return await relay.call(secret, method, request)

    @app.get("/file/bot{secret}/{path:path}")
    async def download(secret: str, path: str) -> Response:
        return await relay.download(secret, path)

    return app


__all__ = ["Relay", "RelayStore", "code_hash", "create_relay_app", "secret_hash"]
