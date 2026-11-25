"""Mail (reply tracking) on the settings page: presets, a write-only password, Test connection, the guards."""

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sarthi.web.actions import MailSettings
from sarthi.web.app import create_app, read_only
from sarthi.web.mail_form import mail_from_form, mail_values
from tests.conftest import FakeClock
from tests.web.test_settings import HERE, FakeActions, token

TYPED_KEY = "abcdefghijklmnop"  # fictional


class MailActions(FakeActions):
    def login_sites(self) -> list[tuple[str, str]]:
        return []

    def screen_link(self) -> str | None:
        return None


@pytest.fixture
def actions() -> MailActions:
    return MailActions()


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock, actions: MailActions) -> TestClient:
    app = create_app(
        read_only(tmp_path / "data" / "sarthi.db"), clock=clock, actions=actions, data_dir=tmp_path
    )
    return TestClient(app, base_url="http://127.0.0.1")


NEW = MailSettings("", 993, "", "INBOX", enabled=False)
GMAIL = MailSettings(
    "imap.gmail.com", 993, "asha.rao@example.com", "INBOX", enabled=True, password_saved=True
)
FORM = {
    "mail_provider": "gmail", "mail_host": "", "mail_port": "993", "mail_user": "asha.rao@example.com",
    "mail_password": "abcd efgh ijkl mnop", "mail_folder": "INBOX", "mail_on": "on",
}  # fmt: skip


def test_a_preset_fills_the_host_and_gmail_spaces_are_dropped() -> None:
    result = mail_from_form(FORM, NEW)
    assert result.settings == MailSettings("imap.gmail.com", 993, "asha.rao@example.com", "INBOX", True)
    assert result.password == TYPED_KEY
    assert "mail_password" not in result.values  # never sent back into the page


def test_a_custom_server() -> None:
    form = FORM | {"mail_provider": "other", "mail_host": "imaps://Mail.Example.org/", "mail_port": "1993"}
    result = mail_from_form(form, NEW)
    assert result.settings is not None and (result.settings.host, result.settings.port) == (
        "mail.example.org",
        1993,
    )


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"mail_provider": "other", "mail_host": "not a host"}, "mail_host"),
        ({"mail_provider": "other", "mail_host": "imap.example.com\r\nA1 STORE"}, "mail_host"),
        ({"mail_provider": "aol"}, "mail_provider"),
        ({"mail_port": "99999"}, "mail_port"),
        ({"mail_port": "abc"}, "mail_port"),
        ({"mail_user": ""}, "mail_user"),
        ({"mail_user": "asha@example.com\r\nA1 LOGOUT"}, "mail_user"),
        ({"mail_password": ""}, "mail_password"),
        ({"mail_password": "pass\r\nA1 DELETE INBOX"}, "mail_password"),
        ({"mail_folder": "INBOX\r\nA1 EXPUNGE"}, "mail_folder"),
        ({"mail_password": "x" * 400}, "mail_password"),
    ],
)
def test_bad_values_are_explained(change: dict[str, str], field: str) -> None:
    result = mail_from_form(FORM | change, NEW)
    assert result.settings is None and field in result.errors


def test_turning_mail_off_needs_nothing_else() -> None:
    result = mail_from_form({"mail_provider": "gmail", "mail_user": "", "mail_password": ""}, NEW)
    assert result.settings is not None and result.settings.enabled is False


def test_an_empty_password_keeps_the_saved_one_for_the_same_mailbox_only() -> None:
    kept = mail_from_form(FORM | {"mail_password": "", "mail_user": "Asha.Rao@example.com"}, GMAIL)
    assert kept.settings is not None and kept.password == ""
    other_account = mail_from_form(FORM | {"mail_password": "", "mail_user": "boss@example.com"}, GMAIL)
    assert "mail_password" in other_account.errors
    other_server = mail_from_form(FORM | {"mail_password": "", "mail_provider": "zoho"}, GMAIL)
    assert "mail_password" in other_server.errors  # the Gmail password is never sent to Zoho


def test_the_form_shows_the_saved_setup() -> None:
    assert mail_values(GMAIL)["mail_provider"] == "gmail" and mail_values(GMAIL)["mail_on"] == "on"
    custom = MailSettings("mail.example.org", 143, "a@example.org", "Jobs", enabled=False)
    values = mail_values(custom)
    assert (values["mail_provider"], values["mail_host"], values["mail_port"]) == (
        "other",
        "mail.example.org",
        "143",
    )


# ---- the page ----


def test_the_section_never_shows_the_password(client: TestClient, actions: FakeActions) -> None:
    actions.mail_settings = GMAIL
    page = client.get("/settings").text
    assert "Mail (reply tracking)" in page and 'action="/settings/mail"' in page
    assert 'formaction="/settings/mail/test"' in page
    assert re.search(r'name="mail_password" type="password" value=""', page)
    assert "Saved. Leave empty to keep it." in page
    assert '<option value="gmail" selected>' in page
    assert "myaccount.google.com/apppasswords" in page


def test_saving_mail_settings(client: TestClient, actions: FakeActions) -> None:
    response = client.post(
        "/settings/mail", data={"csrf": token(client)} | FORM, headers=HERE, follow_redirects=False
    )
    assert (response.status_code, response.headers["location"]) == (303, "/settings?done=mail#mail")
    saved, password = actions.mail_saved[-1]
    assert (saved.host, saved.user, saved.enabled, password) == (
        "imap.gmail.com", "asha.rao@example.com", True, "abcdefghijklmnop",
    )  # fmt: skip
    page = client.get("/settings?done=mail").text
    assert "no restart needed" in page and "abcdefghijklmnop" not in page


def test_a_bad_value_comes_back_without_the_password(client: TestClient, actions: FakeActions) -> None:
    data = {"csrf": token(client)} | FORM | {"mail_port": "nope"}
    response = client.post("/settings/mail", data=data, headers=HERE)
    assert response.status_code == 400 and "usually 993" in response.text
    assert "abcd efgh" not in response.text and 'value="asha.rao@example.com"' in response.text
    assert actions.mail_saved == []


def test_test_connection_reports_in_plain_words_and_saves_nothing(
    client: TestClient, actions: FakeActions
) -> None:
    good = token(client)
    ok = client.post("/settings/mail/test", data={"csrf": good} | FORM, headers=HERE)
    assert ok.status_code == 200 and "Connected: 3 messages in INBOX." in ok.text
    bad = client.post(
        "/settings/mail/test", data={"csrf": good} | FORM | {"mail_password": "wrong"}, headers=HERE
    )
    assert (
        "Could not connect: the mailbox refused the login." in bad.text and 'class="notice bad"' in bad.text
    )
    assert 'value="wrong"' not in bad.text
    assert len(actions.mail_tested) == 2 and actions.mail_saved == []


def test_mail_changes_need_the_token_and_this_computer(client: TestClient, actions: FakeActions) -> None:
    good = token(client)
    for path in ("/settings/mail", "/settings/mail/test"):
        assert client.post(path, data={"csrf": "guess"} | FORM, headers=HERE).status_code == 403
        evil = {"origin": "https://evil.example"}
        assert client.post(path, data={"csrf": good} | FORM, headers=evil).status_code == 403
    assert actions.mail_saved == [] and actions.mail_tested == []
