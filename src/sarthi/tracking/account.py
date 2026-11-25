"""The mailbox reply tracking reads: set on the dashboard (``data/mail.json``) or in ``.env``.

What the dashboard saved wins over ``.env``, field by field: an empty value there (say, no password typed)
leaves the ``.env`` one in force, and its on/off switch decides whether mail is read at all. The file is
readable by this user only; it is never stored in the database, shown back or logged.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr

from sarthi.config import Settings
from sarthi.tracking.handler import Mailbox

MAIL_FILE = "mail.json"
MAX_PORT = 65535


@dataclass(frozen=True, slots=True)
class MailSetup:
    """Where to read mail and whether to: as saved, or as in force."""

    host: str = ""
    port: int = 0
    user: str = ""
    password: SecretStr | None = field(default=None, repr=False)
    folder: str = ""
    enabled: bool | None = None  # None: not decided on the dashboard (``.env`` alone decides)

    def mailbox(self) -> Mailbox | None:
        """The mailbox to read, or None when reading is off or something is missing."""
        if not (self.enabled and self.host and self.user and self.password is not None):
            return None
        return Mailbox(
            self.host, self.port or 993, self.user, self.password.get_secret_value(), self.folder or "INBOX"
        )


def saved_setup(data_dir: Path) -> MailSetup | None:
    """What the dashboard saved, or None (never saved, or the file is unreadable)."""
    try:
        data = json.loads((data_dir / MAIL_FILE).read_text(encoding="utf-8"))
        port = int(data.get("port") or 0)
        password = str(data.get("password") or "")
        enabled = data.get("enabled")
        setup = MailSetup(
            host=str(data.get("host") or ""),
            port=port if 0 < port <= MAX_PORT else 0,
            user=str(data.get("user") or ""),
            password=SecretStr(password) if password else None,
            folder=str(data.get("folder") or ""),
            enabled=enabled if isinstance(enabled, bool) else None,
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return setup


def save_setup(data_dir: Path, setup: MailSetup) -> None:
    """Keep ``setup`` (a None password keeps the saved one), readable by this user only."""
    password = setup.password
    if password is None:
        earlier = saved_setup(data_dir)
        password = earlier.password if earlier is not None else None
    data = {
        "host": setup.host,
        "port": setup.port,
        "user": setup.user,
        "password": password.get_secret_value() if password is not None else "",
        "folder": setup.folder,
        "enabled": bool(setup.enabled),
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    path, part = data_dir / MAIL_FILE, data_dir / f".{MAIL_FILE}.part"
    with contextlib.suppress(FileNotFoundError):
        part.unlink()
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file)
    with contextlib.suppress(OSError):
        part.chmod(0o600)
    part.replace(path)  # all at once: a crash never leaves half a file


def current_setup(settings: Settings, data_dir: Path) -> MailSetup:
    """The setup in force: the dashboard's values over ``.env``'s."""
    saved = saved_setup(data_dir) or MailSetup()
    password = saved.password or settings.mail_password
    return MailSetup(
        host=saved.host or settings.mail_imap_host,
        port=saved.port or settings.mail_imap_port,
        user=saved.user or settings.mail_user,
        password=password,
        folder=saved.folder or settings.mail_folder,
        enabled=saved.enabled if saved.enabled is not None else settings.mail_enabled,
    )


def current_mailbox(settings: Settings, data_dir: Path) -> Mailbox | None:
    """The mailbox to read now (read again before every check, so a dashboard change counts at once)."""
    return current_setup(settings, data_dir).mailbox()
