"""Reading new emails over IMAP: works with Gmail (an app password), Outlook, Zoho or any mailbox.

Read-only: the mailbox is opened with EXAMINE, so nothing is marked read, moved or deleted. Only messages
newer than the last one seen are fetched (by UID), at most ``MAX_PER_RUN`` at a time, each capped in size.
The body is reduced to plain text; attachments are never opened.
"""

from __future__ import annotations

import contextlib
import email
import email.policy
import html
import imaplib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

MAX_PER_RUN = 200
MAX_MESSAGE_BYTES = 2_000_000
BODY_CHARS = 8000
TIMEOUT_S = 30


class MailError(RuntimeError):
    """The mailbox could not be read (the message is safe to show; it never contains the password)."""


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


def _plain(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
        text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)))
    return re.sub("[ \t\u00a0]+", " ", re.sub(r"\n\s*\n+", "\n\n", str(text))).strip()[:BODY_CHARS]


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
        subject=re.sub(r"\s+", " ", str(message.get("Subject", "")))[:300],
        sent_at=sent,
        body=_plain(message),
    )


def fetch_new(  # noqa: PLR0913  (connection details)
    host: str, port: int, user: str, password: str, after_uid: int, *, since: date, folder: str = "INBOX"
) -> list[Mail]:
    """Messages with a UID above ``after_uid`` (oldest first). Blocking: run it in a thread.

    The first read (``after_uid`` 0) takes only messages from ``since`` on, the newest ``MAX_PER_RUN``.
    """
    try:
        box = imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT_S)
    except OSError as error:
        raise MailError(f"could not reach {host}:{port} ({type(error).__name__})") from None
    try:
        try:
            box.login(user, password)
        except imaplib.IMAP4.error:
            raise MailError("the mailbox refused the login (for Gmail, use an app password)") from None
        status, _ = box.select(f'"{folder}"', readonly=True)  # EXAMINE: nothing is changed
        if status != "OK":
            raise MailError(f"folder {folder!r} not found")
        if after_uid:
            status, data = box.uid("SEARCH", f"UID {after_uid + 1}:*")
        else:
            status, data = box.uid("SEARCH", f"SINCE {since.strftime('%d-%b-%Y')}")
        uids = (
            sorted(int(u) for u in (data[0] or b"").split() if int(u) > after_uid) if status == "OK" else []
        )
        uids = uids[:MAX_PER_RUN] if after_uid else uids[-MAX_PER_RUN:]
        mails: list[Mail] = []
        for uid in uids:
            status, parts = box.uid("fetch", str(uid), "(RFC822.SIZE BODY.PEEK[])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            raw = parts[0][1]
            if len(raw) > MAX_MESSAGE_BYTES:
                continue
            mails.append(parse(uid, raw))
    except (imaplib.IMAP4.error, OSError) as error:
        raise MailError(f"reading the mailbox failed ({type(error).__name__})") from None
    else:
        return mails
    finally:
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            box.logout()
