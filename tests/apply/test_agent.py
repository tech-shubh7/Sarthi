"""The application browser in a real headless browser, on local test sites (no network, a scripted AI).

What must hold whatever the AI says: it never types, never presses the final submit, never presses a
forbidden button, never leaves for an unrelated site, and stops at a login wall.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from sarthi.apply.agent import (
    ApplicationBrowser,
    Button,
    LoginWallError,
    Step,
    allowed_link,
    guarded,
    rule_step,
)
from sarthi.apply.engine import ApplyEngine, FillError
from sarthi.jobs.forms import Applicant, FormContext, FormField

FORMS = Path(__file__).parent / "forms"
ASHA = Applicant(
    first_name="Asha", last_name="Rao", email="asha@example.com", country="India", work_authorized=True
)
WHY = {"Why do you want to work here?": "I like the product."}


def url(name: str) -> str:
    return (FORMS / name).as_uri()


class ScriptedAi:
    """Answers by the text of the button it wants; records what it was shown."""

    def __init__(self, state: str = "go", press: str = "", key: str = "") -> None:
        self.state, self.press, self.key = state, press, key
        self.asked: list[tuple[str, str, str]] = []

    async def __call__(self, page_text: str, fields: str, buttons: str) -> tuple[str, str, str] | None:
        self.asked.append((page_text, fields, buttons))
        if self.key:
            return (self.state, self.key, "scripted")
        for line in buttons.splitlines():
            key, text, *_ = (part.strip() for part in line.split("|"))
            if text == self.press:
                return (self.state, key, "scripted")
        return ("stuck", "", "no such button")


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[ApplyEngine]:
    made = ApplyEngine(tmp_path / "shots")
    try:
        await made._new_page()
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"Chromium not installed for Playwright: {error}")
    yield made
    await made.aclose()


@pytest.fixture
def resume(tmp_path: Path) -> Path:
    path = tmp_path / "Asha_Rao_Resume.pdf"
    path.write_bytes(b"%PDF-1.4 test")
    return path


def ctx(resume: Path, answers: dict[str, str] | None = None) -> FormContext:
    return FormContext(ASHA, resume_file=str(resume), answers=WHY if answers is None else answers)


async def test_from_a_job_page_through_two_steps_to_submit_without_any_ai(
    engine: ApplyEngine, resume: Path
) -> None:
    engine.browser = ApplicationBrowser(None)  # rules only: cookie banner, Apply, Next, Submit
    result = await engine.fill("job-1", url("agent_job.html"), ctx(resume))
    filled = dict(result.filled)
    assert result.adapter == "ai"
    assert filled["First name"] == "Asha" and filled["Email"] == "asha@example.com"
    assert filled["Resume/CV"] == "Asha_Rao_Resume.pdf"
    assert filled["Are you legally authorized to work in India?"] == "Yes"
    assert filled["Why do you want to work here?"] == "I like the product."
    assert result.blanks == [] and result.failed == [] and not result.captcha
    assert result.screenshot is not None and result.screenshot.is_file()
    opened = engine._open["job-1"]
    assert "Thank you" not in await opened.page.inner_text("body")  # filled, NOT sent
    assert (await engine.submit("job-1", "WRONG1")).outcome == "expired"
    done = await engine.submit("job-1", result.code)
    assert done.outcome == "submitted", done.detail  # the thanks shown in place, at the same address


async def test_a_question_only_the_user_can_answer_stops_the_walk(engine: ApplyEngine, resume: Path) -> None:
    engine.browser = ApplicationBrowser(None)
    result = await engine.fill("job-2", url("agent_job.html"), ctx(resume, answers={}))
    assert [f.label for f in result.blanks] == ["Why do you want to work here?"]
    assert dict(result.filled)["First name"] == "Asha"  # what could be filled was


async def test_the_ai_is_asked_only_when_the_page_is_unclear(engine: ApplyEngine, resume: Path) -> None:
    ai = ScriptedAi(press="Start")
    engine.browser = ApplicationBrowser(ai)
    result = await engine.fill("job-3", url("agent_pages.html") + "#choice", ctx(resume))
    assert result.adapter == "ai" and not result.blanks
    assert len(ai.asked) == 1  # "Start" or "Learn more": the AI; the form's steps: the rules
    page_text, fields, buttons = ai.asked[0]
    assert "Platform Engineer" in page_text
    assert fields == ""  # no form on the job page (the real AI step says so in words)
    assert "Start" in buttons and "Learn more" in buttons
    assert "asha@example.com" not in "".join(ai.asked[0])  # the AI never sees the applicant's data


async def test_without_an_ai_an_unclear_page_is_handed_over(engine: ApplyEngine, resume: Path) -> None:
    engine.browser = ApplicationBrowser(None)
    with pytest.raises(FillError, match="no AI model"):
        await engine.fill("job-4", url("agent_pages.html") + "#choice", ctx(resume))


async def test_a_page_that_tries_to_steer_the_ai_cannot_make_it_press_anything_bad(
    engine: ApplyEngine, resume: Path
) -> None:
    for bad in ("Delete account",):
        engine.browser = ApplicationBrowser(ScriptedAi(press=bad))
        with pytest.raises(FillError, match="refused to press"):
            await engine.fill("job-5", url("agent_pages.html") + "#trick", ctx(resume))
    # "Send" pressed as "go" is held as the final press: the walk stops, nothing is sent.
    engine.browser = ApplicationBrowser(ScriptedAi(press="Send"))
    result = await engine.fill("job-6", url("agent_pages.html") + "#trick", ctx(resume))
    page = engine._open["job-6"].page
    assert await page.title() == "Test pages"  # neither "deleted" nor "sent"
    assert result.code


async def test_the_ai_cannot_name_a_button_that_is_not_on_the_page(engine: ApplyEngine, resume: Path) -> None:
    engine.browser = ApplicationBrowser(ScriptedAi(key="b999"))
    with pytest.raises(FillError, match="named no button"):
        await engine.fill("job-7", url("agent_pages.html") + "#choice", ctx(resume))


async def test_a_login_wall_goes_to_the_user(engine: ApplyEngine, resume: Path) -> None:
    engine.browser = ApplicationBrowser(None)
    with pytest.raises(LoginWallError, match="sign in") as raised:
        await engine.fill("job-8", url("agent_pages.html") + "#login", ctx(resume))
    assert not raised.value.account


async def test_an_apply_link_to_another_site_is_not_followed(engine: ApplyEngine, resume: Path) -> None:
    engine.browser = ApplicationBrowser(None)
    with pytest.raises(FillError, match="another site"):
        await engine.fill("job-9", url("agent_pages.html") + "#away", ctx(resume))


async def test_a_form_in_a_new_tab_is_followed(engine: ApplyEngine, resume: Path) -> None:
    engine.browser = ApplicationBrowser(None)
    result = await engine.fill("job-10", url("agent_pages.html") + "#tab", ctx(resume))
    assert result.adapter == "ai" and not result.blanks and not result.failed
    assert engine._open["job-10"].page.url.endswith("agent_form.html")


async def test_on_a_board_with_a_login_apply_itself_waits_for_approval(
    engine: ApplyEngine, resume: Path
) -> None:
    engine.browser = ApplicationBrowser(None)
    result = await engine.fill("job-11", url("agent_job.html"), ctx(resume), one_click=True)
    page = engine._open["job-11"].page
    assert page.url.endswith("agent_job.html")  # "Apply for this job" was not pressed: it may send at once
    assert result.code


# ---- the rules, without a browser ----


def b(key: str, text: str, where: str = "page", href: str = "") -> Button:
    return Button(key, text, where, href)


FIELD = FormField("s-1", "Email", "email", True)


@pytest.mark.parametrize(
    ("buttons", "fields", "expected"),
    [
        ([b("b1", "Apply with LinkedIn"), b("b2", "Apply for this job")], [], ("go", "b2")),
        ([b("b1", "Apply now"), b("b2", "Apply now")], [], ("go", "b1")),  # the same button twice
        ([b("b1", "Apply"), b("b2", "Apply manually")], [], None),  # two ways in: the AI chooses
        ([b("b1", "Back", "form"), b("b2", "Next", "form")], [FIELD], ("go", "b2")),
        ([b("b1", "Back", "form"), b("b2", "Submit application", "form")], [FIELD], ("ready", "b2")),
        ([b("b1", "Continue", "form"), b("b2", "Submit", "form")], [FIELD], ("ready", "b2")),  # final first
        ([b("b1", "Sign in", "form")], [FIELD], None),
        ([b("b1", "Share"), b("b2", "Save job")], [], None),
    ],
)
def test_rule_steps(buttons: list[Button], fields: list[FormField], expected: tuple[str, str] | None) -> None:
    step = rule_step(buttons, fields, "", one_click=False)
    assert (step.state, step.button.key) == expected if expected else step is None  # type: ignore[union-attr]


def test_a_cookie_banner_is_accepted_first() -> None:
    step = rule_step([b("b1", "Apply now"), b("b2", "Accept all")], [], "We use cookies.", one_click=False)
    assert step is not None and step.button is not None and step.button.text == "Accept all"


@pytest.mark.parametrize(
    ("text", "state"),
    [
        ("Submit application", "ready"),
        ("Send", "ready"),
        ("Finish", "ready"),
        ("Next", "go"),
        ("Sign in with Google", "stuck"),
        ("Create account", "stuck"),
        ("Log out", "stuck"),
        ("Delete my profile", "stuck"),
    ],
)
def test_an_ai_step_is_held_to_the_rules(text: str, state: str) -> None:
    assert guarded(Step("go", b("b1", text)), one_click=False).state == state


def test_on_a_board_an_ai_apply_is_a_final_press() -> None:
    assert guarded(Step("go", b("b1", "Easy Apply")), one_click=True).state == "ready"
    assert guarded(Step("go", b("b1", "Easy Apply")), one_click=False).state == "go"


@pytest.mark.parametrize(
    ("href", "ok"),
    [
        ("", True),
        ("#apply", True),
        ("javascript:void(0)", True),
        ("https://careers.acme.com/apply/1", True),  # the same site as the job page
        ("https://boards.greenhouse.io/acme/jobs/1", True),  # a hiring system
        ("https://acme.wd3.myworkdayjobs.com/x", True),  # followed, then handed over (account)
        ("https://evil.example/apply", False),
        ("mailto:jobs@acme.com", False),
        ("tel:+911234", False),
        ("https://acme.com.evil.example/apply", False),  # looks alike, is not
        ("https://jobs.acme.co.in/1", False),  # another company's .co.in site
    ],
)
def test_links_the_walk_may_follow(href: str, ok: bool) -> None:
    assert allowed_link(href, "https://www.acme.com/jobs/1", "https://www.acme.com/jobs/1") is ok


def test_co_in_sites_are_compared_whole() -> None:
    assert allowed_link(
        "https://careers.acme.co.in/apply", "https://acme.co.in/jobs/1", "https://acme.co.in/jobs/1"
    )
    assert not allowed_link(
        "https://other.co.in/apply", "https://acme.co.in/jobs/1", "https://acme.co.in/jobs/1"
    )
