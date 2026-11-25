"""The mailbox set on the dashboard (data/mail.json) over .env, and a running tracker picking it up."""

import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from sarthi.config import Settings
from sarthi.db.applications import ApplicationStore
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.db.replies import ReplyStore
from sarthi.queue import names
from sarthi.queue.tasks import TaskQueue
from sarthi.tracking.account import MAIL_FILE, MailSetup, current_mailbox, current_setup, save_setup
from sarthi.tracking.handler import ReplyTracker
from sarthi.tracking.mail import MailBatch
from tests.conftest import FakeClock

SECRET, OTHER_SECRET = "env-app-pass", "dash-app-pass"  # fictional


def settings(tmp_path: Path, **values: Any) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, **values)


def test_env_alone_still_works(tmp_path: Path) -> None:
    env = settings(
        tmp_path,
        mail_imap_host="imap.example.com",
        mail_user="asha@example.com",
        mail_password=SecretStr(SECRET),
    )
    box = current_mailbox(env, tmp_path)
    assert box is not None and (box.host, box.port, box.password, box.folder) == (
        "imap.example.com", 993, SECRET, "INBOX",
    )  # fmt: skip
    assert current_mailbox(settings(tmp_path), tmp_path) is None  # nothing set: off


def test_the_dashboard_wins_and_an_empty_value_keeps_env(tmp_path: Path) -> None:
    env = settings(
        tmp_path,
        mail_imap_host="imap.example.com",
        mail_user="asha@example.com",
        mail_password=SecretStr(SECRET),
    )
    save_setup(tmp_path, MailSetup("imap.example.org", 1993, "", None, "Jobs", enabled=True))
    box = current_mailbox(env, tmp_path)
    assert box is not None
    assert (box.host, box.port, box.user, box.password, box.folder) == (
        "imap.example.org", 1993, "asha@example.com", SECRET, "Jobs",
    )  # fmt: skip
    save_setup(
        tmp_path, MailSetup("imap.example.org", 1993, "", SecretStr(OTHER_SECRET), "Jobs", enabled=True)
    )
    assert current_mailbox(env, tmp_path).password == OTHER_SECRET  # type: ignore[union-attr]
    save_setup(tmp_path, MailSetup("imap.example.org", 1993, "", None, "Jobs", enabled=False))
    assert current_mailbox(env, tmp_path) is None  # off on the dashboard beats .env
    assert current_setup(env, tmp_path).password == SecretStr(OTHER_SECRET)  # the saved password stays


def test_the_file_is_this_users_only_and_survives_junk(tmp_path: Path) -> None:
    save_setup(
        tmp_path, MailSetup("imap.example.org", 993, "a@example.org", SecretStr(OTHER_SECRET), "", True)
    )
    path = tmp_path / MAIL_FILE
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.part"))
    path.write_text("{not json")
    assert current_mailbox(settings(tmp_path), tmp_path) is None  # unreadable: as if never saved
    path.write_text('{"port": "x", "enabled": "yes"}')
    assert current_setup(settings(tmp_path), tmp_path).port == 993


def test_repr_never_shows_the_password() -> None:
    assert OTHER_SECRET not in repr(MailSetup("h", 993, "u", SecretStr(OTHER_SECRET), "INBOX", True))


async def test_a_running_tracker_follows_the_dashboard(
    conn: sqlite3.Connection, job_store: JobStore, queue: TaskQueue, clock: FakeClock, tmp_path: Path
) -> None:
    env = settings(tmp_path)
    calls: list[tuple[Any, ...]] = []

    def fetch(*args: Any, **kwargs: Any) -> MailBatch:
        calls.append(args)
        return MailBatch(5, [], 40)

    track = ReplyTracker(
        ReplyStore(conn, clock=clock), ApplicationStore(conn, clock=clock), job_store,
        ChatStore(conn, clock=clock), queue, ai=None,  # type: ignore[arg-type]
        mailbox=lambda: current_mailbox(env, tmp_path), fetch=fetch, clock=clock,
    )  # fmt: skip
    track.schedule()
    assert queue.active(names.MAIL_CHECK) == 0 and await track.check_mail() == 0 and calls == []
    save_setup(
        tmp_path, MailSetup("imap.example.org", 993, "a@example.org", SecretStr(OTHER_SECRET), "", True)
    )
    track.schedule()  # what the dashboard does on save
    assert queue.active(names.MAIL_CHECK) == 1
    await track.check_mail()
    assert calls[-1][:5] == ("imap.example.org", 993, "a@example.org", OTHER_SECRET, 0)
    await track.check_mail()
    assert calls[-1][4] == 40  # the position is kept for this mailbox...
    save_setup(
        tmp_path, MailSetup("imap.example.org", 993, "b@example.org", SecretStr(OTHER_SECRET), "", True)
    )
    await track.check_mail()
    assert calls[-1][4] == 0  # ...and another mailbox starts from its own beginning
