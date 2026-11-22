"""Using the shared Sarthi bot through its relay, instead of a bot of your own.

On first start this Sarthi registers with the relay and keeps the secret it is given in ``data/relay.json``
(readable by this user only). The secret takes the place of a bot token in every call; the relay lets it reach
only the one chat that opened this Sarthi's pairing link (see ``sarthi.relay.server``).
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import SecretStr

LINK_FILE = "relay.json"
PAIR_LINK_KEY = "telegram.pair_link"  # the link to connect Telegram, while no chat is connected
MODE_KEY = "telegram.mode"  # "own" or "relay:<url>": a change resets the chat link and update offset
REGISTER_TIMEOUT_S = 20.0


class RelayError(Exception):
    """The relay could not be reached or refused to register this Sarthi."""


@dataclass(frozen=True, slots=True)
class RelayLink:
    """This Sarthi's registration with a relay."""

    url: str
    secret: SecretStr
    bot: str  # the shared bot's username, for t.me links

    def pairing_link(self, code: str) -> str:
        """The link that connects the user's Telegram chat to this Sarthi."""
        return f"https://t.me/{self.bot}?start={code}"


def _read(path: Path, url: str) -> RelayLink | None:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict) or saved.get("url") != url or not saved.get("secret"):
        return None  # another relay (or a broken file): register again
    return RelayLink(url, SecretStr(str(saved["secret"])), str(saved.get("bot") or ""))


def _write(path: Path, link: RelayLink) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"url": link.url, "secret": link.secret.get_secret_value(), "bot": link.bot}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def relay_link(data_dir: Path, url: str, http: httpx.Client | None = None) -> RelayLink:
    """The saved registration with the relay at ``url``, or a new one (one request, the first time only)."""
    url = url.rstrip("/")
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise RelayError("the relay address must start with https:// (plain http only on this computer)")
    path = data_dir / LINK_FILE
    if (saved := _read(path, url)) is not None:
        return saved
    client = http or httpx.Client(timeout=REGISTER_TIMEOUT_S)
    try:
        response = client.post(f"{url}/v1/register")
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise RelayError(f"could not reach it ({type(error).__name__})") from None
    finally:
        if http is None:
            client.close()
    result = body.get("result") if isinstance(body, dict) and body.get("ok") else None
    if not isinstance(result, dict) or not result.get("secret"):
        description = body.get("description") if isinstance(body, dict) else None
        raise RelayError(str(description or f"HTTP {response.status_code}")[:200])
    link = RelayLink(url, SecretStr(str(result["secret"])), str(result.get("bot") or ""))
    _write(path, link)
    return link
