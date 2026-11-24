"""The apply engine in a real headless browser, against local test forms (no network)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from sarthi.apply.adapters import adapter_for, apply_url, is_workday
from sarthi.apply.engine import ApplyEngine, FillError
from sarthi.jobs.forms import Applicant, FormContext

FORMS = Path(__file__).parent / "forms"
ASHA = Applicant(
    first_name="Asha", last_name="Rao", email="asha@example.com", country="India", work_authorized=True
)


def url(name: str) -> str:
    return (FORMS / name).as_uri()


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


async def test_fill_then_submit_with_the_code(engine: ApplyEngine, resume: Path) -> None:
    ctx = FormContext(
        ASHA, resume_file=str(resume), answers={"Why do you want to work here?": "I like the product."}
    )
    result = await engine.fill("job-1", url("basic.html"), ctx)
    filled = dict(result.filled)
    assert filled["First name"] == "Asha"
    assert filled["Resume/CV"] == "Asha_Rao_Resume.pdf"
    assert filled["Are you legally authorized to work in India?"] == "Yes"
    assert filled["Gender"] == "I don't wish to answer"
    assert result.blanks == [] and result.failed == []
    assert result.screenshot is not None and result.screenshot.is_file()
    assert len(result.code) == 6
    assert not engine.can_submit("job-1", "WRONG1")
    assert (await engine.submit("job-1", "WRONG1")).outcome == "expired"
    done = await engine.submit("job-1", result.code.lower())
    assert done.outcome == "submitted", done.detail
    assert (await engine.submit("job-1", result.code)).outcome == "expired"  # single use


async def test_required_questions_without_an_answer_block_submitting(
    engine: ApplyEngine, resume: Path
) -> None:
    result = await engine.fill("job-2", url("basic.html"), FormContext(ASHA, resume_file=str(resume)))
    assert [f.label for f in result.blanks] == ["Why do you want to work here?"]
    assert (await engine.submit("job-2", result.code)).outcome == "needs_human"


async def test_refilling_voids_the_old_code(engine: ApplyEngine, resume: Path) -> None:
    ctx = FormContext(ASHA, resume_file=str(resume), answers={"Why do you want to work here?": "Yes."})
    first = await engine.fill("job-3", url("basic.html"), ctx)
    second = await engine.fill("job-3", url("basic.html"), ctx)
    assert not engine.can_submit("job-3", first.code) or first.code == second.code
    assert engine.can_submit("job-3", second.code)


async def test_a_captcha_is_handed_to_the_user(engine: ApplyEngine) -> None:
    result = await engine.fill(
        "job-4", url("captcha.html"), FormContext(ASHA, answers={"Full name": "Asha Rao"})
    )
    assert result.captcha
    assert (await engine.submit("job-4", result.code)).outcome == "needs_human"


async def test_no_confirmation_is_a_failure_not_a_success(
    engine: ApplyEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sarthi.apply.engine.SUBMIT_WAIT_S", 2.0)
    result = await engine.fill("job-5", url("broken.html"), FormContext(ASHA))
    outcome = await engine.submit("job-5", result.code)
    assert outcome.outcome == "failed"


async def test_a_page_that_does_not_load_is_a_fill_error(engine: ApplyEngine) -> None:
    with pytest.raises(FillError, match="did not load"):
        await engine.fill("job-6", "http://127.0.0.1:9/nothing-here", FormContext(ASHA))


@pytest.mark.parametrize(
    ("given", "adapter", "page"),
    [
        (
            "https://job-boards.greenhouse.io/acme/jobs/1",
            "greenhouse",
            "https://job-boards.greenhouse.io/acme/jobs/1",
        ),
        ("https://jobs.lever.co/acme/123", "lever", "https://jobs.lever.co/acme/123/apply"),
        ("https://jobs.lever.co/acme/123/apply", "lever", "https://jobs.lever.co/acme/123/apply"),
        (
            "https://jobs.ashbyhq.com/acme/9?src=x",
            "ashby",
            "https://jobs.ashbyhq.com/acme/9/application?src=x",
        ),
        ("https://careers.acme.com/job?gh_jid=5", "greenhouse", "https://careers.acme.com/job?gh_jid=5"),
        ("https://careers.acme.com/job/5", "generic", "https://careers.acme.com/job/5"),
    ],
)
def test_adapters_and_form_pages(given: str, adapter: str, page: str) -> None:
    assert adapter_for(given).name == adapter
    assert apply_url(given) == page


def test_workday_is_recognised() -> None:
    assert is_workday("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1")
    assert not is_workday("https://myworkdayjobs.com.evil.example/x")


async def test_watch_sees_the_users_own_submit(engine: ApplyEngine) -> None:
    await engine.fill("job-7", url("captcha.html"), FormContext(ASHA, answers={"Full name": "Asha Rao"}))
    page = engine._open["job-7"].page

    async def user_sends() -> None:
        await asyncio.sleep(0.5)
        await page.evaluate("document.body.innerHTML = '<h1>Thank you for applying!</h1>'")

    sending = asyncio.create_task(user_sends())
    done = await engine.watch("job-7", wait_s=10)
    await sending
    assert done.outcome == "submitted", done.detail
    assert done.screenshot is not None and done.screenshot.is_file()


async def test_watch_gives_up_and_never_presses_submit(engine: ApplyEngine) -> None:
    await engine.fill("job-8", url("captcha.html"), FormContext(ASHA, answers={"Full name": "Asha Rao"}))
    done = await engine.watch("job-8", wait_s=1)
    assert done.outcome == "needs_human"
    assert "thank" not in (await engine._open["job-8"].page.content()).lower()
    assert (await engine.watch("no-such-job", wait_s=1)).outcome == "expired"


async def test_watch_stops_when_the_window_is_closed(engine: ApplyEngine) -> None:
    await engine.fill("job-9", url("captcha.html"), FormContext(ASHA, answers={"Full name": "Asha Rao"}))
    await engine._open["job-9"].page.close()
    done = await engine.watch("job-9", wait_s=5)
    assert done.outcome == "needs_human" and "closed" in done.detail
