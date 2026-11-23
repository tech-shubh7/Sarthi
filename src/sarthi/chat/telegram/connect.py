"""One-tap Telegram setup: the user gets a Telegram bot of their own without @BotFather or a token to copy.

Telegram's "managed bots" (Bot API 9.6) let a manager bot have bots created for users. The Sarthi connector
(``deploy/telegram-connect``, a Cloudflare Worker) is that manager's webhook. This side:

1. asks the connector for a setup session and shows its link (dashboard and log);
2. the user opens it in Telegram and taps "Create my Sarthi bot";
3. collects the new bot's token from the connector (handed over once, then erased there) and keeps it in
   ``data/telegram_bot.json``, readable by this user only.

From then on this Sarthi runs that bot directly: the connector is not involved in any message.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import SecretStr

BOT_FILE = "telegram_bot.json"
STEP_KEY = "telegram.pair_step"  # "create" (open the connector link) or "start" (tap Start in your bot)
TIMEOUT_S = 20.0
_TOKEN = re.compile(r"^\d{3,20}:[A-Za-z0-9_-]{30,80}$")
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class ConnectError(Exception):
    """The connector could not be reached, or refused."""


@dataclass(frozen=True, slots=True)
class ManagedBot:
    """The user's own bot, made for them through the connector."""

    token: SecretStr
    username: str
    owner_id: int  # the Telegram user who created it: the only person it should serve

    def start_link(self, code: str) -> str:
        """The link that opens this bot and links the user's chat to this Sarthi."""
        return f"https://t.me/{self.username}?start={code}"


@dataclass(frozen=True, slots=True)
class Session:
    """One setup in progress."""

    id: str
    secret: SecretStr
    link: str  # t.me link to the manager bot with this session's code


def saved_bot(data_dir: Path) -> ManagedBot | None:
    """The bot made earlier, or None (never made, or the file is unreadable)."""
    try:
        data = json.loads((data_dir / BOT_FILE).read_text(encoding="utf-8"))
        bot = ManagedBot(SecretStr(str(data["token"])), str(data["username"]), int(data["owner_id"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    valid = _TOKEN.match(bot.token.get_secret_value()) and _USERNAME.match(bot.username)
    return bot if valid else None


def save_bot(data_dir: Path, bot: ManagedBot) -> None:
    """Keep the bot's token, readable by this user only."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / BOT_FILE
    data = {"token": bot.token.get_secret_value(), "username": bot.username, "owner_id": bot.owner_id}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


class Connector:
    """Talks to the Sarthi connector at ``url``."""

    def __init__(self, url: str, http: httpx.AsyncClient | None = None) -> None:
        """Use the connector at ``url`` (https, or plain http on this computer for tests)."""
        url = url.rstrip("/")
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ConnectError("the connector address must start with https://")
        self.url = url
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_S)

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    async def _json(self, method: str, path: str, headers: dict[str, str] | None = None) -> dict[str, object]:
        try:
            response = await self._http.request(method, f"{self.url}{path}", headers=headers)
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ConnectError(f"could not reach it ({type(error).__name__})") from None
        if not isinstance(body, dict):
            raise ConnectError(f"unexpected answer (HTTP {response.status_code})")
        if not body.get("ok"):
            raise ConnectError(str(body.get("error") or f"HTTP {response.status_code}")[:200])
        return body

    async def session(self) -> Session:
        """Start a setup: the link to show the user."""
        body = await self._json("POST", "/v1/sessions")
        session_id, secret, link = (
            str(body.get("id", "")),
            str(body.get("secret", "")),
            str(body.get("start_link", "")),
        )
        if not session_id or not secret or not link.startswith("https://t.me/"):
            raise ConnectError("the connector sent an incomplete session")
        return Session(session_id, SecretStr(secret), link)

    async def collect(self, session: Session) -> ManagedBot | None:
        """The user's new bot once they created it; None while they have not yet."""
        auth = {"authorization": f"Bearer {session.secret.get_secret_value()}"}
        body = await self._json("GET", f"/v1/sessions/{session.id}", auth)
        if body.get("status") != "ready":
            return None
        try:
            bot = ManagedBot(
                SecretStr(str(body["token"])), str(body["bot_username"]), int(str(body["owner_id"]))
            )
        except (KeyError, ValueError, TypeError):
            raise ConnectError("the connector sent an incomplete bot") from None
        if not _TOKEN.match(bot.token.get_secret_value()) or not _USERNAME.match(bot.username):
            raise ConnectError("the connector sent something that is not a bot token")
        return bot
