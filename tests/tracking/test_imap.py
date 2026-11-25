"""Reading a real IMAP server (a fake one on 127.0.0.1, over TLS): what ``fetch_new`` sends and gets back.

Fictional mail only. Checks: each email is read once, every common encoding is decoded, attachments are
never read, a wrong password and a server that is down are told in plain words (never with the password),
the server's certificate is verified, and nothing in the mailbox is ever changed or marked read.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import ssl
from collections.abc import Iterator
from datetime import UTC, date, datetime
from email.message import EmailMessage
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sarthi.ai.replies import ReplyAnswer
from sarthi.db.applications import ApplicationStatus, ApplicationStore
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.db.replies import ReplyStore
from sarthi.queue import names
from sarthi.queue.tasks import PermanentError, RetryableError, TaskQueue
from sarthi.tracking.handler import STATUS_KEY, Mailbox, ReplyTracker
from sarthi.tracking.mail import (
    MAX_MESSAGE_BYTES,
    MailError,
    MailLoginError,
    count_messages,
    fetch_new,
    imap_date,
    imap_utf7,
)
from tests.conftest import FakeClock
from tests.matching.conftest import make_job
from tests.tracking.imap_server import Certificate, ImapServer, Mailstore, closed_port, make_certificate

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl tool")

USER, PASSWORD = "asha.rao@example.com", "abcd-efgh-ijkl-mnop"
WRONG, NOT_ASCII = "wrong-secret-9", "pässwörd"  # fictional
SINCE = date(2026, 10, 21)
TODAY = date(2026, 11, 20)


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> Certificate:
    return make_certificate(tmp_path_factory.mktemp("tls"))


@pytest.fixture
def store() -> Mailstore:
    return Mailstore(USER, PASSWORD)


@pytest.fixture
def server(store: Mailstore, certificate: Certificate) -> Iterator[ImapServer]:
    with ImapServer(store, certificate) as running:
        yield running


def read(server: ImapServer, certificate: Certificate, after: int = 0, **kwargs: Any) -> Any:
    return fetch_new(
        "localhost", server.port, USER, kwargs.pop("password", PASSWORD), after,
        since=SINCE, context=certificate.client_context(), **kwargs,
    )  # fmt: skip


def email_bytes(subject: str = "Hello", body: str = "Hi Asha", **headers: str) -> bytes:
    message = EmailMessage()
    message["From"] = headers.get("sender", "Example Labs Talent <talent@example.com>")
    message["To"] = USER
    message["Subject"] = subject
    message["Date"] = headers.get("when", "Fri, 20 Nov 2026 10:00:00 +0000")
    message["Message-ID"] = headers.get("message_id", f"<{abs(hash(subject + body))}@example.com>")
    message.set_content(body)
    return bytes(message)


# ---- reading ---------------------------------------------------------------------------------------------


def test_new_mail_is_read_once_and_never_twice(
    server: ImapServer, store: Mailstore, certificate: Certificate
) -> None:
    store.add(email_bytes("Old news"), date(2026, 9, 1))  # before the first look-back: never read
    first = store.add(email_bytes("Thank you for applying"), date(2026, 11, 18))
    second = store.add(email_bytes("Interview invitation"), TODAY)
    batch = read(server, certificate)
    assert [m.uid for m in batch.mails] == [first, second]
    assert (batch.validity, batch.last_uid) == (store.validity, second)
    again = read(server, certificate, after=batch.last_uid, validity=batch.validity)
    assert again.mails == []  # the server answers "n:*" with the newest one: it is not read again
    third = store.add(email_bytes("Coding test"), TODAY)
    later = read(server, certificate, after=batch.last_uid, validity=batch.validity)
    assert [m.subject for m in later.mails] == ["Coding test"]
    assert later.last_uid == third


def test_a_renumbered_folder_is_read_afresh(
    server: ImapServer, store: Mailstore, certificate: Certificate
) -> None:
    uid = store.add(email_bytes("Interview invitation"), TODAY)
    store.validity = 99  # the server rebuilt the folder: old UIDs mean nothing
    batch = read(server, certificate, after=uid + 500, validity=7)
    assert [m.uid for m in batch.mails] == [uid]
    assert batch.validity == 99


def test_an_empty_folder(server: ImapServer, certificate: Certificate, store: Mailstore) -> None:
    batch = read(server, certificate, after=40, validity=store.validity)
    assert (batch.mails, batch.last_uid) == ([], 40)
    assert "UID SEARCH" not in store.names()  # some servers refuse a UID range in an empty folder


def test_encodings_are_decoded(server: ImapServer, store: Mailstore, certificate: Certificate) -> None:
    store.add(
        b"From: =?UTF-8?B?UHJpeWEgU2hhcm1h?= <talent@example.com>\r\n"
        b"Subject: =?UTF-8?Q?Einladung_zum_Vorstellungsgespr=C3=A4ch_=E2=80=93_Backend?=\r\n"
        b"Date: Fri, 20 Nov 2026 10:00:00 +0100\r\nMessage-ID: <qp@example.com>\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"Gr=C3=BC=C3=9Fe Asha, w=C3=A4hlen Sie einen Termin.\r\n",
        TODAY,
    )
    store.add(
        b"From: hr@example.org\r\nSubject: =?ISO-8859-1?Q?Caf=E9_chat?=\r\n"
        b"Date: Fri, 20 Nov 2026 11:00:00 +0000\r\nMessage-ID: <b64@example.org>\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"4KSo4KSu4KS44KWN4KSk4KWHIEFzaGEsIGludGVydmlldyBvbiBNb25kYXku\r\n",
        TODAY,
    )
    store.add(
        b"From: hr@example.org\r\nSubject: Broken charset\r\nMessage-ID: <odd@example.org>\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=x-unknown-8\r\n\r\nPlease reply soon.\r\n",
        TODAY,
    )
    qp, b64, odd = read(server, certificate).mails
    assert qp.subject == "Einladung zum Vorstellungsgespräch – Backend"
    assert qp.sender_name == "Priya Sharma" and qp.sender == "talent@example.com"
    assert qp.body == "Grüße Asha, wählen Sie einen Termin."
    assert qp.sent_at == datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    assert (b64.subject, b64.body) == ("Café chat", "नमस्ते Asha, interview on Monday.")
    assert odd.body == "Please reply soon."  # an unknown charset: still read


def test_html_only_mail_and_attachments(
    server: ImapServer, store: Mailstore, certificate: Certificate
) -> None:
    html = EmailMessage()
    html["From"], html["Subject"], html["Message-ID"] = "talent@example.com", "Next steps", "<h@example.com>"
    html.set_content(
        "<html><head><title>Mailer</title><style>p{color:red}</style></head><body><p>Hi&nbsp;Asha,</p>"
        "<p>Please book a <b>technical interview</b>.<br>Thanks</p><script>track()</script></body></html>",
        subtype="html",
    )
    store.add(bytes(html), TODAY)
    offer = EmailMessage()
    offer["From"], offer["Subject"], offer["Message-ID"] = "hr@example.com", "Your offer", "<o@example.com>"
    offer.set_content("Your offer letter is attached.")
    offer.add_attachment(
        b"%PDF-1.7 SECRET-PDF-CONTENT", maintype="application", subtype="pdf", filename="o.pdf"
    )
    offer.add_attachment(b"x" * (MAX_MESSAGE_BYTES + 500_000), maintype="application", subtype="zip")
    store.add(bytes(offer), TODAY)
    first, second = read(server, certificate).mails
    assert first.body == "Hi Asha,\nPlease book a technical interview .\nThanks"
    assert "Mailer" not in first.body and "track" not in first.body and "color" not in first.body
    # An email bigger than the cap (a large attachment) is still read: its text comes first.
    assert second.body == "Your offer letter is attached."
    assert "SECRET-PDF-CONTENT" not in second.body
    fetches = [c for c in store.commands if c.startswith("UID FETCH")]
    assert all(f"BODY.PEEK[]<0.{MAX_MESSAGE_BYTES}>" in c for c in fetches)


def test_a_folder_other_than_the_inbox(
    server: ImapServer, store: Mailstore, certificate: Certificate
) -> None:
    # Servers name folders in modified UTF-7: "Entw&APw-rfe" on the wire.
    store.add(email_bytes("Interview invitation"), TODAY, folder="Bewerbungen/Antworten &- Entw&APw-rfe")
    batch = read(server, certificate, folder="Bewerbungen/Antworten & Entwürfe")
    assert [m.subject for m in batch.mails] == ["Interview invitation"]
    with pytest.raises(MailError, match="no folder 'Jobs'"):
        read(server, certificate, folder="Jobs")


def test_imap_dates_and_folder_names_do_not_follow_the_computers_language() -> None:
    assert imap_date(date(2026, 3, 5)) == "05-Mar-2026"
    assert imap_utf7("Entwürfe") == "Entw&APw-rfe"
    assert imap_utf7("A & B") == "A &- B"
    hindi = imap_utf7("नौकरी")
    assert hindi.isascii() and hindi.replace("&", "+").replace(",", "/").encode().decode("utf-7") == "नौकरी"


# ---- read-only ---------------------------------------------------------------------------------------------


def test_nothing_is_ever_changed_or_marked_read(
    server: ImapServer, store: Mailstore, certificate: Certificate
) -> None:
    for subject in ("One", "Two", "Three"):
        store.add(email_bytes(subject), TODAY)
    batch = read(server, certificate)
    read(server, certificate, after=batch.last_uid, validity=batch.validity)
    count_messages("localhost", server.port, USER, PASSWORD, context=certificate.client_context())
    assert store.writes() == []  # EXAMINE, never SELECT; no STORE, COPY, MOVE, EXPUNGE...
    assert "EXAMINE" in store.names()
    assert all(not m.flags for m in store.folders["INBOX"])  # no \Seen: the user still sees them as new


# ---- failures ----------------------------------------------------------------------------------------------


def test_a_wrong_password_is_explained_without_the_password(
    server: ImapServer, store: Mailstore, certificate: Certificate, caplog: pytest.LogCaptureFixture
) -> None:
    store.echo_password = True  # even a server that repeats it back
    caplog.set_level(logging.DEBUG)
    with pytest.raises(MailLoginError) as caught:
        read(server, certificate, password=WRONG)
    text = str(caught.value)
    assert "refused the login" in text and "Invalid credentials" in text and "app password" in text
    assert WRONG not in text and WRONG not in caplog.text
    assert store.names()[-1] == "LOGOUT"  # the connection is closed properly


def test_a_password_with_characters_imap_cannot_send(server: ImapServer, certificate: Certificate) -> None:
    with pytest.raises(MailLoginError, match="characters") as caught:
        read(server, certificate, password=NOT_ASCII)
    assert NOT_ASCII not in str(caught.value)


def test_a_server_that_is_down_or_unknown(certificate: Certificate) -> None:
    port = closed_port()
    with pytest.raises(MailError, match=f"127.0.0.1:{port} refused the connection") as caught:
        fetch_new("127.0.0.1", port, USER, PASSWORD, 0, since=SINCE, context=certificate.client_context())
    assert not isinstance(caught.value, MailLoginError)  # retryable, unlike a wrong password
    with pytest.raises(MailError, match=r"no mail server called imap\.invalid"):
        fetch_new("imap.invalid", 993, USER, PASSWORD, 0, since=SINCE)


def test_the_servers_certificate_is_always_verified(server: ImapServer, store: Mailstore) -> None:
    # Python's imaplib trusts any certificate unless told otherwise; Sarthi always verifies.
    with pytest.raises(MailError, match="certificate of localhost could not be verified"):
        fetch_new("localhost", server.port, USER, PASSWORD, 0, since=SINCE)
    assert "LOGIN" not in store.names()  # the password never left this side
    wrong_name = ssl.create_default_context(cafile=None)
    with pytest.raises(MailError, match="could not be verified"):
        fetch_new("localhost", server.port, USER, PASSWORD, 0, since=SINCE, context=wrong_name)


def test_count_messages_for_the_dashboards_test(
    server: ImapServer, store: Mailstore, certificate: Certificate
) -> None:
    store.add(email_bytes("One"), TODAY)
    store.add(email_bytes("Two"), TODAY)
    context = certificate.client_context()
    assert count_messages("localhost", server.port, USER, PASSWORD, context=context) == 2
    with pytest.raises(MailLoginError):
        count_messages("localhost", server.port, USER, "nope", context=context)


# ---- the whole way: an employer's email to a Telegram notice ----------------------------------------------


class FakeAi:
    available = True

    def __init__(self, answer: ReplyAnswer) -> None:
        self.answer = answer
        self.prompts: list[dict[str, Any]] = []

    async def structured(self, prompt: Any, schema: Any, **data: Any) -> Any:
        self.prompts.append(data)
        return self.answer


def tracker_for(
    server: ImapServer | None, certificate: Certificate, conn: sqlite3.Connection, job_store: JobStore,
    queue: TaskQueue, clock: FakeClock, ai: FakeAi, password: str = PASSWORD,
) -> ReplyTracker:  # fmt: skip
    port = server.port if server is not None else closed_port()
    return ReplyTracker(
        ReplyStore(conn, clock=clock),
        ApplicationStore(conn, clock=clock),
        job_store,
        ChatStore(conn, clock=clock),
        queue,
        ai=ai,  # type: ignore[arg-type]
        mailbox=lambda: Mailbox("localhost", port, USER, password),
        fetch=partial(fetch_new, context=certificate.client_context()),
        clock=clock,
    )


@pytest.fixture
def applied(conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock) -> str:
    apps = ApplicationStore(conn, clock=clock)
    job_id = job_store.save(
        make_job(url="https://careers.example.org/jobs/42", company="Example Labs", title="Backend Engineer")
    ).job_id
    apps.record(job_id, ApplicationStatus.SUBMITTED, site="careers.example.org")
    other = job_store.save(
        make_job(url="https://jobs.example.net/7", company="Other Works", title="Data Analyst")
    ).job_id
    apps.record(other, ApplicationStatus.SUBMITTED, site="jobs.example.net")
    return job_id


async def test_an_interview_email_becomes_a_notice_for_the_right_application(
    server: ImapServer, store: Mailstore, certificate: Certificate, conn: sqlite3.Connection,
    job_store: JobStore, queue: TaskQueue, clock: FakeClock, applied: str,
) -> None:  # fmt: skip
    store.add(
        email_bytes(
            "Interview invitation for Backend Engineer at Example Labs",
            "Hi Asha,\n\nWe would like to invite you to a technical interview on 2 December at 15:00.\n",
            message_id="<invite-1@example.com>",
        ),
        TODAY,
    )
    store.add(email_bytes("Weekly newsletter", "Ten tips", sender="news@example.net"), TODAY)
    ai = FakeAi(
        ReplyAnswer(kind="interview", confidence=0.92, summary="Technical interview", dates=["2 Dec"])
    )
    track = tracker_for(server, certificate, conn, job_store, queue, clock, ai)
    assert await track.check_mail() == 1
    task = queue.claim([names.REPLY_NOTICE])
    assert task is not None and task.key == "<invite-1@example.com>"
    assert task.payload["job"] == applied
    assert task.payload["kind"] == "interview" and task.payload["dates"] == ["2 Dec"]
    assert task.payload["subject"] == "Interview invitation for Backend Engineer at Example Labs"
    assert ReplyStore(conn).latest_kind(applied) == "interview"
    assert len(ai.prompts) == 1  # the newsletter never reached the AI
    # The next check reads nothing again, and the mailbox is as it was.
    assert await track.check_mail() == 0
    assert queue.claim([names.REPLY_NOTICE]) is None
    assert store.writes() == [] and all(not m.flags for m in store.folders["INBOX"])
    assert '"ok": true' in (ChatStore(conn).get_state(STATUS_KEY) or "")


async def test_a_wrong_password_waits_for_the_next_check_and_a_down_server_retries(
    server: ImapServer, certificate: Certificate, conn: sqlite3.Connection, job_store: JobStore,
    queue: TaskQueue, clock: FakeClock, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    ai = FakeAi(ReplyAnswer(kind="other", confidence=0.9))
    caplog.set_level(logging.DEBUG)
    wrong = tracker_for(server, certificate, conn, job_store, queue, clock, ai, password=WRONG)
    with pytest.raises(PermanentError, match="refused the login"):  # not hammered: accounts get locked
        await wrong.check_mail()
    status = ChatStore(conn).get_state(STATUS_KEY) or ""
    assert "refused the login" in status and WRONG not in status + caplog.text
    down = tracker_for(None, certificate, conn, job_store, queue, clock, ai)
    with pytest.raises(RetryableError, match="refused the connection"):
        await down.check_mail()


def test_the_mailbox_never_shows_its_password() -> None:
    box = Mailbox("imap.example.com", 993, USER, "s3cret-app-password")
    assert "s3cret-app-password" not in repr(box)


# ---- the dashboard's Save and Test connection (the running app's side) ------------------------------------


async def test_the_dashboard_saves_tests_and_turns_mail_on_without_a_restart(
    server: ImapServer, store: Mailstore, certificate: Certificate, conn: sqlite3.Connection,
    job_store: JobStore, queue: TaskQueue, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    import sarthi.app as app_module  # noqa: PLC0415
    from sarthi.config import Settings  # noqa: PLC0415
    from sarthi.tracking.account import current_mailbox  # noqa: PLC0415
    from sarthi.web.actions import MailSettings  # noqa: PLC0415

    store.add(email_bytes("One"), TODAY)
    monkeypatch.setattr(
        app_module, "count_messages", partial(count_messages, context=certificate.client_context())
    )
    settings = Settings(_env_file=None, data_dir=tmp_path)
    chats = ChatStore(conn, clock=clock)
    tracker = ReplyTracker(
        ReplyStore(conn, clock=clock), ApplicationStore(conn, clock=clock), job_store, chats, queue,
        ai=None,  # type: ignore[arg-type]
        mailbox=lambda: current_mailbox(settings, tmp_path), clock=clock,
    )  # fmt: skip
    fake_app = SimpleNamespace(settings=settings, bot=SimpleNamespace(chats=chats), tracker=tracker)
    web = app_module.WebActions.__new__(app_module.WebActions)
    web._app = fake_app  # type: ignore[assignment]
    assert web.mail().enabled is False and web.mail().password_saved is False
    typed = MailSettings("localhost", server.port, USER, "INBOX", enabled=True)
    assert await web.test_mail(typed, PASSWORD) == "Connected: 1 message in INBOX."
    assert (await web.test_mail(typed, WRONG)).startswith("Could not connect: the mailbox refused the login")
    assert await web.test_mail(typed, "") == "Add the app password to test the connection."
    assert queue.active(names.MAIL_CHECK) == 0
    web.save_mail(typed, PASSWORD)
    assert queue.active(names.MAIL_CHECK) == 1  # the first check is queued at once
    shown = web.mail()
    assert (shown.host, shown.port, shown.enabled, shown.password_saved) == (
        "localhost",
        server.port,
        True,
        True,
    )
    assert PASSWORD not in repr(shown)
    assert await web.test_mail(typed, "") == "Connected: 1 message in INBOX."  # the saved one, same mailbox
    moved = MailSettings("localhost", server.port, "boss@example.com", "INBOX", enabled=True)
    assert await web.test_mail(moved, "") == "Add the app password to test the connection."
    assert store.writes() == []
