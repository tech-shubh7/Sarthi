"""Reading new emails over IMAP: works with Gmail (an app password), Yahoo, Zoho, iCloud or any mailbox.

Read-only: the folder is opened with EXAMINE and messages are read with ``BODY.PEEK``, so nothing is marked
read, moved or deleted. Only messages newer than the last one seen are fetched (by UID, within the folder's
UIDVALIDITY), at most ``MAX_PER_RUN`` at a time, and only the first ``MAX_MESSAGE_BYTES`` of each (the text
comes before attachments). The body is reduced to plain text; attachments are never opened.

The server's certificate is always verified (Python's ``imaplib`` does not do that by default), so the
password is never sent to a machine posing as the mail server.
"""

from __future__ import annotations

import base64
import contextlib
import email
import email.policy
import html
import imaplib
import logging
import re
import socket
import ssl
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

log = logging.getLogger(__name__)

MAX_PER_RUN = 200
MAX_MESSAGE_BYTES = 2_000_000
BODY_CHARS = 8000
TIMEOUT_S = 30
SERVER_TEXT_CHARS = 160
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FOLDER = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_PRINTABLE_FIRST, _PRINTABLE_LAST = 0x20, 0x7E


class MailError(RuntimeError):
    """The mailbox could not be read (the message is safe to show; it never contains the password)."""


class MailLoginError(MailError):
    """The mailbox refused the user name or password: trying again soon will not help."""


@dataclass(frozen=True, slots=True)
class Mail:
    """One email, reduced to what tracking needs."""

    uid: int
    message_id: str
    sender: str  # address only
    sender_name: str
    subject: str
    sent_at: datetime | None
    body: str  # plain text


@dataclass(frozen=True, slots=True)
class MailBatch:
    """What one read found."""

    validity: int  # the folder's UIDVALIDITY: UIDs under another value are other messages
    mails: list[Mail] = field(default_factory=list)
    last_uid: int = 0  # the highest UID looked at, read or skipped (an unreadable email is not tried again)


def _plain(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):  # an unknown or wrong charset: the bytes, read as well as they can be
        payload = part.get_payload(decode=True)
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(script|style|head)\b.*?</\1\s*>", " ", text, flags=re.S | re.I)
        text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)))
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in str(text).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:BODY_CHARS]


def parse(uid: int, raw: bytes) -> Mail:
    """One raw message as a ``Mail``."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(message, EmailMessage)  # noqa: S101  (policy.default always builds EmailMessage)
    name, address = parseaddr(str(message.get("From", "")))
    try:
        sent = parsedate_to_datetime(str(message.get("Date", "")))
        sent = sent if sent.tzinfo else sent.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        sent = None
    return Mail(
        uid=uid,
        message_id=str(message.get("Message-ID", "") or f"uid-{uid}").strip()[:300],
        sender=address.lower()[:200],
        sender_name=name[:200],
        subject=re.sub(r"\s+", " ", str(message.get("Subject", ""))).strip()[:300],
        sent_at=sent,
        body=_plain(message),
    )


# ---- IMAP -----------------------------------------------------------------------------------------------


def imap_date(day: date) -> str:
    """``24-Nov-2026``: IMAP's date, in English whatever the computer's language."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def imap_utf7(name: str) -> str:
    """A folder name in IMAP's modified UTF-7 (RFC 3501 5.1.3): "Entwürfe" -> "Entw&APw-rfe"."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            encoded = base64.b64encode("".join(run).encode("utf-16-be")).decode("ascii")
            out.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
            run.clear()

    for char in name:
        if _PRINTABLE_FIRST <= ord(char) <= _PRINTABLE_LAST:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            run.append(char)
    flush()
    return "".join(out)


def _quoted_folder(folder: str) -> str:
    if not _FOLDER.match(folder):
        raise MailError("the folder name is empty or has characters a folder cannot have")
    encoded = imap_utf7(folder)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unreachable(host: str, port: int, error: Exception) -> str:  # noqa: PLR0911  (one sentence per cause)
    """Why the server could not be reached, in plain words."""
    if isinstance(error, ssl.SSLCertVerificationError):
        return (
            f"the certificate of {host} could not be verified, so Sarthi did not send your password "
            "(check the server name)"
        )
    if isinstance(error, ssl.SSLError):
        return f"{host}:{port} did not start a secure connection (IMAP over SSL uses port 993)"
    if isinstance(error, socket.gaierror):
        return f"no mail server called {host} was found (check the spelling and your internet connection)"
    if isinstance(error, ConnectionRefusedError):
        return f"{host}:{port} refused the connection (check the port: IMAP over SSL uses 993)"
    if isinstance(error, TimeoutError):
        return f"{host}:{port} did not answer within {TIMEOUT_S} seconds"
    if isinstance(error, imaplib.IMAP4.error):
        return f"{host}:{port} does not answer like an IMAP mail server"
    return f"could not reach {host}:{port} ({type(error).__name__})"


def _server_text(error: imaplib.IMAP4.error, *secrets: str) -> str:
    """The server's own reason (e.g. "Invalid credentials"), short, printable, and never a secret."""
    said = error.args[0] if error.args else ""
    text = said.decode("utf-8", "replace") if isinstance(said, bytes) else str(said)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return re.sub(r"[^\x20-\x7e]+", " ", text).strip()[:SERVER_TEXT_CHARS]


@contextlib.contextmanager
def _session(  # noqa: PLR0913  (connection details)
    host: str, port: int, user: str, password: str, folder: str, *, context: ssl.SSLContext | None
) -> Generator[tuple[imaplib.IMAP4_SSL, int, int]]:
    """Logged in with ``folder`` open read-only: (the connection, messages in it, its UIDVALIDITY)."""
    name = _quoted_folder(folder)
    try:
        box = imaplib.IMAP4_SSL(
            host, port, ssl_context=context or ssl.create_default_context(), timeout=TIMEOUT_S
        )
    except (OSError, imaplib.IMAP4.error) as error:
        raise MailError(_unreachable(host, port, error)) from None
    try:
        try:
            box.login(user, password)
        except UnicodeEncodeError:
            raise MailLoginError(
                "the user name or password has characters a mail server cannot take (type it as shown)"
            ) from None
        except imaplib.IMAP4.error as error:
            reason = _server_text(error, password)
            raise MailLoginError(
                f"the mailbox refused the login ({reason or 'no reason given'}). Check the email address, "
                "and use an app password, not your normal password"
            ) from None
        status, data = box.select(name, readonly=True)  # EXAMINE: nothing is changed
        if status != "OK":
            raise MailError(f"there is no folder {folder!r} in this mailbox")
        count = int(data[0] or 0) if data and isinstance(data[0], bytes) else 0
        _, said = box.response("UIDVALIDITY")
        validity = int(said[0]) if said and isinstance(said[0], bytes) and said[0].isdigit() else 0
        yield box, count, validity
    except (imaplib.IMAP4.error, OSError, ValueError) as error:
        what = _unreachable(host, port, error) if isinstance(error, OSError) else type(error).__name__
        raise MailError(f"reading the mailbox failed ({what})") from None
    finally:
        with contextlib.suppress(imaplib.IMAP4.error, OSError, ValueError):
            box.logout()


def count_messages(  # noqa: PLR0913  (connection details)
    host: str,
    port: int,
    user: str,
    password: str,
    folder: str = "INBOX",
    *,
    context: ssl.SSLContext | None = None,
) -> int:
    """Log in, open ``folder`` read-only and say how many messages it has (the dashboard's "Test")."""
    with _session(host, port, user, password, folder, context=context) as (_, count, _validity):
        return count


def _one(box: imaplib.IMAP4_SSL, uid: int) -> Mail | None:
    """One message by UID (its first ``MAX_MESSAGE_BYTES``, never marked read), or None if unreadable."""
    status, parts = box.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{MAX_MESSAGE_BYTES}>)")
    raw = next(
        (p[1] for p in parts or [] if isinstance(p, tuple) and len(p) > 1 and isinstance(p[1], bytes)), None
    )
    if status != "OK" or raw is None:
        return None
    try:
        return parse(uid, raw)
    except Exception:  # noqa: BLE001  (one malformed email must not stop every later one)
        log.warning("email %d could not be read and was skipped", uid)
        return None


def fetch_new(  # noqa: PLR0913  (connection details)
    host: str,
    port: int,
    user: str,
    password: str,
    after_uid: int,
    *,
    since: date,
    folder: str = "INBOX",
    validity: int = 0,
    context: ssl.SSLContext | None = None,
) -> MailBatch:
    """Messages with a UID above ``after_uid`` (oldest first). Blocking: run it in a thread.

    ``after_uid`` counts only under the same ``validity`` (the folder's UIDVALIDITY the last read saw): when
    the server renumbered the folder, it is read again as a first read. A first read (``after_uid`` 0) takes
    only messages from ``since`` on, the newest ``MAX_PER_RUN``. ``context`` is for tests only.
    """
    with _session(host, port, user, password, folder, context=context) as (box, count, current):
        if validity and current and current != validity:
            after_uid = 0
        if count == 0:  # (some servers refuse a UID range in an empty folder)
            return MailBatch(current, [], after_uid)
        criteria = f"UID {after_uid + 1}:*" if after_uid else f"SINCE {imap_date(since)}"
        status, data = box.uid("SEARCH", criteria)
        if status != "OK":
            raise MailError("the mail server refused the search for new emails")
        found = sorted({int(u) for u in (data[0] or b"").split()} if data else set())
        uids = [u for u in found if u > after_uid]  # "n:*" also returns the newest when none is above n
        uids = uids[:MAX_PER_RUN] if after_uid else uids[-MAX_PER_RUN:]
        mails = [mail for mail in (_one(box, uid) for uid in uids) if mail is not None]
        return MailBatch(current, mails, max([after_uid, *uids]))
