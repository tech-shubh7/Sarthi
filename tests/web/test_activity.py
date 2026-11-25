"""The Activity page: questions, auto-apply's veto and what Sarthi did, all without Telegram."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sarthi.db.answers import AnswerStore
from sarthi.db.chat import ChatStore
from sarthi.db.jobs import JobStore
from sarthi.db.matches import MatchRecord, MatchStore
from sarthi.db.notices import NoticeStore
from sarthi.jobs.forms import FormField
from sarthi.queue import names
from sarthi.queue.tasks import TaskQueue
from sarthi.web.app import create_app, read_only
from tests.conftest import FakeClock
from tests.matching.conftest import make_job
from tests.web.test_settings import HERE, FakeActions, token


@pytest.fixture
def actions() -> FakeActions:
    return FakeActions()


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_path: Path, clock: FakeClock, actions: FakeActions) -> TestClient:
    app = create_app(
        read_only(tmp_path / "data" / "sarthi.db"), clock=clock, actions=actions, data_dir=tmp_path
    )
    return TestClient(app, base_url="http://127.0.0.1")


def waiting_job(
    conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock, queue: TaskQueue, n: int = 1
) -> str:
    job_id = job_store.save(
        make_job(url=f"https://jobs.lever.co/acme/{n}", title=f"ML Engineer {n}", company="Acme Labs")
    ).job_id
    MatchStore(conn, clock=clock).save(MatchRecord(job_id, "v", 91, "alert", "strong", None, (), (), True))
    return job_id


def test_questions_auto_applies_and_notices_are_listed(
    client: TestClient, conn: sqlite3.Connection, job_store: JobStore, clock: FakeClock, queue: TaskQueue
) -> None:
    asked = waiting_job(conn, job_store, clock, queue, 1)
    AnswerStore(conn, clock=clock).set_pending(
        asked,
        [
            FormField("visa", "Do you need a visa?", "select", True, ("Yes", "No")),
            FormField("tools", "Which tools?", "checkbox", True, ("Python", "Go")),
            FormField("why", "Why <us>?", "textarea", True),
        ],
    )
    auto = waiting_job(conn, job_store, clock, queue, 2)
    ChatStore(conn, clock=clock).set_auto(auto, on=True)
    queue.enqueue(names.AUTO_APPLY, auto, delay=300)
    NoticeStore(conn, clock=clock).add("followup", "⏰ No reply yet.\n<pre>Dear team, &lt;b&gt;</pre>", asked)

    page = client.get("/activity").text
    assert "Do you need a visa?" in page and '<option value="No"' in page
    assert 'type="checkbox" name="a.tools" value="Go"' in page
    assert "Why &lt;us&gt;?" in page  # labels from the employer's form are text, never markup
    assert f'action="/auto/{auto}/cancel"' in page and "91% match" in page
    assert "Dear team, &lt;b&gt;" in page and "Follow-up" in page
    overview = client.get("/").text
    assert "Waiting for you" in overview and "Jobs whose form asks you something" in overview
    assert "Auto-applies you can still cancel" in overview


def test_answers_go_to_the_running_app_and_problems_come_back(
    client: TestClient, actions: FakeActions, conn: sqlite3.Connection, job_store: JobStore,
    clock: FakeClock, queue: TaskQueue,
) -> None:  # fmt: skip
    job_id = waiting_job(conn, job_store, clock, queue)
    AnswerStore(conn, clock=clock).set_pending(
        job_id, [FormField("visa", "Do you need a visa?", "select", True, ("Yes", "No"))]
    )
    csrf = token(client, "/activity")
    data = {"csrf": csrf, "a.visa": "Maybe", "a.tools": ["Python", "Go"]}
    bad = client.post(f"/questions/{job_id}", data=data, headers=HERE)
    assert bad.status_code == 400 and "Please pick one of the listed answers." in bad.text
    assert actions.answered[-1] == (job_id, {"visa": "Maybe", "tools": "Python, Go"})
    good = client.post(
        f"/questions/{job_id}", data={"csrf": csrf, "a.visa": "No"}, headers=HERE, follow_redirects=False
    )
    assert good.status_code == 303 and "done=answered" in good.headers["location"]
    skip = client.post(
        f"/questions/{job_id}", data={"csrf": csrf, "decline": "1"}, headers=HERE, follow_redirects=False
    )
    assert "done=declined" in skip.headers["location"] and actions.declined == [job_id]


def test_auto_apply_can_be_cancelled_or_started_here(client: TestClient, actions: FakeActions) -> None:
    csrf = token(client)
    for action in ("cancel", "now"):
        sent = client.post(f"/auto/j1/{action}", data={"csrf": csrf}, headers=HERE, follow_redirects=False)
        assert sent.status_code == 303
    assert actions.auto == [("cancel", "j1"), ("now", "j1")]
    assert client.post("/auto/j1/explode", data={"csrf": csrf}, headers=HERE).status_code == 404


def test_site_logins_from_settings(client: TestClient, actions: FakeActions) -> None:
    page = client.get("/settings").text
    assert 'action="/settings/login/naukri"' in page and "Log in to LinkedIn" in page
    csrf = token(client)
    sent = client.post("/settings/login/naukri", data={"csrf": csrf}, headers=HERE, follow_redirects=False)
    assert sent.status_code == 303 and "done=login" in sent.headers["location"]
    assert client.post("/settings/login/myspace", data={"csrf": csrf}, headers=HERE).status_code == 404
    assert actions.logins == ["naukri"]
    actions.screen = "http://127.0.0.1:6080/vnc.html?token=x"
    assert "http://127.0.0.1:6080/vnc.html?token=x" in client.get("/applications").text  # where to log in


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/questions/j1", HERE),
        ("/auto/j1/cancel", HERE),
        ("/settings/login/naukri", {"origin": "https://evil.example"}),
    ],
)
def test_every_new_change_needs_the_token_and_this_computer(
    client: TestClient, actions: FakeActions, path: str, headers: dict[str, str]
) -> None:
    csrf = token(client) if headers is not HERE else "wrong"
    assert client.post(path, data={"csrf": csrf}, headers=headers).status_code == 403
    assert actions.answered == [] and actions.auto == [] and actions.logins == []
