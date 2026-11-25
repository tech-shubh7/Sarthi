"""A small IMAP4rev1 server for tests: TLS on 127.0.0.1, the commands a mail reader uses, all recorded.

It behaves like real servers where Sarthi's reading depends on it: ``UID n:*`` returns the newest message even
when none is above n (RFC 3501), FETCH ``BODY[]`` (without PEEK) and SELECT mark messages read, and a partial
``BODY.PEEK[]<0.n>`` sends only the first n bytes. Nothing leaves this computer.
"""

from __future__ import annotations

import re
import socket
import socketserver
import ssl
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Commands that change a mailbox, or open it so that reading changes it (SELECT, unlike EXAMINE).
WRITES = frozenset(
    {"SELECT", "STORE", "UID STORE", "COPY", "UID COPY", "MOVE", "UID MOVE", "EXPUNGE", "UID EXPUNGE",
     "APPEND", "CREATE", "DELETE", "RENAME", "SUBSCRIBE", "UNSUBSCRIBE", "CLOSE"}
)  # fmt: skip


@dataclass
class Message:
    """One stored email."""

    uid: int
    raw: bytes
    day: date
    flags: set[str] = field(default_factory=set)


@dataclass
class Mailstore:
    """What the server holds, and every command it received (LOGIN's password masked)."""

    user: str
    password: str
    folders: dict[str, list[Message]] = field(default_factory=lambda: {"INBOX": []})
    validity: int = 7
    echo_password: bool = False  # a hostile server that repeats the password in its refusal
    commands: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, raw: bytes, day: date, folder: str = "INBOX") -> int:
        """Store an email; returns its UID."""
        with self.lock:
            box = self.folders.setdefault(folder, [])
            uid = max((m.uid for m in box), default=100) + 1
            box.append(Message(uid, raw, day))
            return uid

    def names(self) -> list[str]:
        """The commands received, without tags or arguments ("LOGIN", "EXAMINE", "UID FETCH", ...)."""
        return [
            c.split(" ", 1)[0] if not c.startswith("UID ") else " ".join(c.split(" ")[:2])
            for c in self.commands
        ]

    def writes(self) -> list[str]:
        """Every command received that could change the mailbox."""
        return [name for name in self.names() if name in WRITES]


def _tokens(text: str) -> list[str]:
    """IMAP arguments: atoms, quoted strings (with \\ escapes) and parenthesised lists kept whole."""
    found: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == " ":
            i += 1
        elif text[i] == '"':
            out, i = [], i + 1
            while i < len(text) and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                out.append(text[i])
                i += 1
            found.append("".join(out))
            i += 1
        elif text[i] == "(":
            end = text.index(")", i)
            found.append(text[i : end + 1])
            i = end + 1
        else:
            end = text.find(" ", i)
            end = len(text) if end < 0 else end
            found.append(text[i:end])
            i = end
    return found


class _Session(socketserver.StreamRequestHandler):
    server: _Server

    def send(self, line: str | bytes) -> None:
        self.wfile.write((line.encode() if isinstance(line, str) else line) + b"\r\n")

    def handle(self) -> None:
        store = self.server.store
        try:
            self.connection.do_handshake()
        except (ssl.SSLError, OSError):
            return  # the client refused the certificate
        self.send("* OK [CAPABILITY IMAP4rev1] Fake IMAP ready")
        folder: list[Message] | None = None
        while line := self.rfile.readline(65536):
            tag, _, rest = line.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            command, _, args = rest.partition(" ")
            command = command.upper()
            if command == "UID":
                sub, _, args = args.partition(" ")
                command = f"UID {sub.upper()}"
            with store.lock:
                store.commands.append(f"{command} ***" if command == "LOGIN" else f"{command} {args}")
            if command == "LOGOUT":
                self.send("* BYE see you")
                self.send(f"{tag} OK LOGOUT completed")
                return
            with store.lock:
                folder = self.run(store, tag, command, args, folder)

    def run(
        self, store: Mailstore, tag: str, command: str, args: str, folder: list[Message] | None
    ) -> list[Message] | None:
        if command == "CAPABILITY":
            self.send("* CAPABILITY IMAP4rev1")
        elif command == "NOOP":
            pass
        elif command == "LOGIN":
            user, password = [*_tokens(args), "", ""][:2]
            if (user, password) != (store.user, store.password):
                said = f" for {password}" if store.echo_password else ""
                self.send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials{said} (Failure)")
                return folder
        elif command in {"EXAMINE", "SELECT"}:
            name = (_tokens(args) or [""])[0]
            if name not in store.folders:
                self.send(f"{tag} NO [NONEXISTENT] Unknown mailbox")
                return None
            folder = store.folders[name]
            self.send(f"* {len(folder)} EXISTS")
            self.send("* 0 RECENT")
            self.send(f"* OK [UIDVALIDITY {store.validity}] UIDs valid")
            self.send("* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)")
            mode = "READ-ONLY" if command == "EXAMINE" else "READ-WRITE"
            self.send(f"{tag} OK [{mode}] {command} completed")
            return folder
        elif command == "UID SEARCH" and folder is not None:
            self.send("* SEARCH " + " ".join(str(m.uid) for m in self.search(folder, args)))
        elif command == "UID FETCH" and folder is not None:
            uid_text, _, items = args.partition(" ")
            for seq, message in enumerate(folder, 1):
                if str(message.uid) == uid_text:
                    self.fetch(seq, message, items.upper())
        elif command in {"UID STORE", "STORE"} and folder is not None:
            folder[0].flags.add("\\Seen")  # what a careless client would cause
        else:
            self.send(f"{tag} BAD unknown or out-of-place command")
            return folder
        self.send(f"{tag} OK {command} completed")
        return folder

    @staticmethod
    def search(folder: list[Message], args: str) -> list[Message]:
        if m := re.fullmatch(r"UID (\d+):\*", args.strip(), re.I):
            above = [msg for msg in folder if msg.uid >= int(m.group(1))]
            return above or folder[-1:]  # n:* is *:n, so the newest message always matches
        if m := re.fullmatch(r"SINCE (\d{1,2})-([A-Za-z]{3})-(\d{4})", args.strip(), re.I):
            since = date(int(m.group(3)), MONTHS.index(m.group(2).title()) + 1, int(m.group(1)))
            return [msg for msg in folder if msg.day >= since]
        raise ValueError(f"search not supported: {args}")

    def fetch(self, seq: int, message: Message, items: str) -> None:
        part = re.search(r"BODY(\.PEEK)?\[\](?:<(\d+)\.(\d+)>)?", items)
        if part is None:
            self.send(f"* {seq} FETCH (UID {message.uid} RFC822.SIZE {len(message.raw)})")
            return
        if not part.group(1):
            message.flags.add("\\Seen")  # BODY[] without PEEK marks the email read
        data, label = message.raw, "BODY[]"
        if part.group(2) is not None:
            start, size = int(part.group(2)), int(part.group(3))
            data, label = message.raw[start : start + size], f"BODY[]<{start}>"
        self.wfile.write(f"* {seq} FETCH (UID {message.uid} {label} {{{len(data)}}}\r\n".encode() + data)
        self.send(")")


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store: Mailstore, context: ssl.SSLContext) -> None:
        super().__init__(("127.0.0.1", 0), _Session)
        self.store = store
        self.context = context

    def get_request(self) -> tuple[Any, Any]:
        sock, address = self.socket.accept()
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address


@dataclass(frozen=True)
class Certificate:
    """A self-signed certificate for localhost and 127.0.0.1."""

    cert: Path
    key: Path

    def client_context(self) -> ssl.SSLContext:
        """A normal, verifying client context that trusts this certificate (and nothing else)."""
        return ssl.create_default_context(cafile=str(self.cert))


def make_certificate(folder: Path) -> Certificate:
    """A fresh self-signed certificate (made with the openssl tool: no key is ever stored in the repo)."""
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(  # noqa: S603  (fixed arguments)
        ["openssl", "req",  # noqa: S607  (the openssl tool on PATH, as on every test machine)
         "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True, timeout=60,
    )  # fmt: skip
    return Certificate(cert, key)


class ImapServer:
    """A running fake server: ``with ImapServer(store, cert) as server: server.port``."""

    def __init__(self, store: Mailstore, certificate: Certificate) -> None:
        """Serve ``store`` over TLS with ``certificate``."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate.cert, certificate.key)
        self.store = store
        self._server = _Server(store, context)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> ImapServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def closed_port() -> int:
    """A port on 127.0.0.1 where nothing listens."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
