"""The mailbox (reply tracking) as a web form, and a submitted form back to settings.

The password is write-only: it is never put back into the page, and an empty one keeps the saved one, but
only for the same mailbox (a saved password is never sent to another server or account).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from sarthi.web.actions import MailSettings

OTHER = "other"
PRESETS = {
    "gmail": ("Gmail", "imap.gmail.com"),
    "outlook": ("Outlook.com / Microsoft 365", "outlook.office365.com"),
    "yahoo": ("Yahoo Mail", "imap.mail.yahoo.com"),
    "zoho": ("Zoho Mail", "imap.zoho.com"),
    "zoho_in": ("Zoho Mail India (zoho.in)", "imap.zoho.in"),
    "icloud": ("iCloud Mail", "imap.mail.me.com"),
}
DEFAULT_PORT = 993
MAX_PORT = 65535
_HOST = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MAX_USER, _MAX_PASSWORD, _MAX_FOLDER = 254, 256, 200


@dataclass
class MailForm:
    """The settings when the form was valid, else the problems (field -> message); the typed values."""

    settings: MailSettings | None
    password: str = ""  # as typed ("" keeps the saved one); never in ``values``
    errors: dict[str, str] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)


def mail_values(current: MailSettings | None) -> dict[str, str]:
    """The setup as the form's fields."""
    if current is None:
        return {"mail_provider": "gmail", "mail_port": str(DEFAULT_PORT), "mail_folder": "INBOX"}
    preset = next((key for key, (_, host) in PRESETS.items() if host == current.host), "")
    return {
        "mail_provider": preset or (OTHER if current.host else "gmail"),
        "mail_host": "" if preset else current.host,
        "mail_port": str(current.port or DEFAULT_PORT),
        "mail_user": current.user,
        "mail_folder": current.folder or "INBOX",
        "mail_on": "on" if current.enabled else "",
    }


def mail_from_form(form: Mapping[str, str], current: MailSettings, *, testing: bool = False) -> MailForm:
    """The mailbox settings, or what is wrong with them. ``testing``: for "Test connection" (needs it all)."""
    values = {
        key: str(form.get(key, "")).strip()[:300]
        for key in ("mail_provider", "mail_host", "mail_port", "mail_user", "mail_folder", "mail_on")
    }
    values["mail_on"] = "on" if values["mail_on"] else ""
    password = str(form.get("mail_password", ""))[: _MAX_PASSWORD + 1]
    errors: dict[str, str] = {}
    provider = values["mail_provider"]
    if provider in PRESETS:
        host = PRESETS[provider][1]
        values["mail_host"] = ""
    elif provider == OTHER:
        host = values["mail_host"].lower().removeprefix("imaps://").removeprefix("imap://").rstrip("/")
    else:
        host = ""
        errors["mail_provider"] = "Pick your mail provider, or Other."
    needed = testing or bool(values["mail_on"])
    if provider == OTHER and (host or needed) and not _HOST.match(host):
        errors["mail_host"] = "Write the IMAP server's name, e.g. imap.example.com."
    port_text = values["mail_port"] or str(DEFAULT_PORT)
    port = int(port_text) if port_text.isdigit() else 0
    if not 0 < port <= MAX_PORT:
        errors["mail_port"] = "A port is a number, usually 993."
    user = values["mail_user"]
    if (needed and not user) or len(user) > _MAX_USER or _CONTROL.search(user) or " " in user:
        errors["mail_user"] = "Write the email address you log in with."
    if provider == "gmail":
        password = password.replace(" ", "")  # Google shows app passwords in groups of four
    if len(password) > _MAX_PASSWORD or _CONTROL.search(password):
        errors["mail_password"] = "That does not look like a password: paste it again."  # noqa: S105  (a message)
        password = ""
    same_box = (host, user.lower()) == (current.host, current.user.lower())
    if needed and not password and not (current.password_saved and same_box):
        errors["mail_password"] = (
            "Add the app password for this mailbox."
            if current.password_saved
            else "Add the app password (it is saved on this computer only)."
        )
    folder = values["mail_folder"] or "INBOX"
    if len(folder) > _MAX_FOLDER or _CONTROL.search(folder):
        errors["mail_folder"] = "Write a folder name, e.g. INBOX."
    if errors:
        return MailForm(None, "", errors, values)
    settings = MailSettings(host, port, user, folder, bool(values["mail_on"]), current.password_saved)
    return MailForm(settings, password, {}, values)
