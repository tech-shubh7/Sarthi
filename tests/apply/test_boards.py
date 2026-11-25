"""LinkedIn Easy Apply and Naukri apply against local pages shaped like theirs (no network, no real account)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from sarthi.apply import external, linkedin, naukri
from sarthi.apply.adapters import FillError, confirmed
from sarthi.apply.engine import ApplyEngine
from sarthi.jobs.forms import Applicant, FormContext

FORMS = Path(__file__).parent / "forms"
ASHA = Applicant(
    first_name="Asha",
    last_name="Rao",
    email="asha@example.com",
    phone="+91 98765 43210",
    work_authorized=True,
)


def url(name: str) -> str:
    return (FORMS / name).as_uri()


@pytest.fixture
async def page(tmp_path: Path) -> AsyncIterator[Any]:
    engine = ApplyEngine(tmp_path / "shots", url_check=None)  # local test pages
    try:
        made = await engine._new_page()
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"Chromium not installed for Playwright: {error}")
    yield made
    await engine.aclose()


@pytest.fixture
def resume(tmp_path: Path) -> str:
    path = tmp_path / "Asha_Rao_Resume.pdf"
    path.write_bytes(b"%PDF-1.4 test")
    return str(path)


async def test_easy_apply_walks_to_the_submit_step_and_stops(page: Any, resume: str) -> None:
    ctx = FormContext(
        ASHA, resume_file=resume, answers={"How many years of experience do you have with Python?": "1"}
    )
    await linkedin.open_easy_apply(page, url("linkedin.html"))
    result = await linkedin.walk(page, ctx, resume)
    assert result.ready, result.note
    filled = dict(result.filled)
    assert filled["Mobile phone number"] == "+91 98765 43210"
    assert "Email address" not in filled  # LinkedIn prefilled it: left as it is
    assert filled["Resume"] == "Asha_Rao_Resume.pdf"
    assert filled["Are you legally authorized to work in India?"] == "Yes"
    assert not await page.locator("#follow-company").is_checked()  # "follow company" unticked
    await page.locator(linkedin.LINKEDIN.submit).click()  # what the user's approval does
    assert await confirmed(page, linkedin.LINKEDIN)


async def test_easy_apply_stops_for_questions_only_the_user_can_answer(page: Any, resume: str) -> None:
    await linkedin.open_easy_apply(page, url("linkedin.html"))
    result = await linkedin.walk(page, FormContext(ASHA, resume_file=resume), resume)
    assert not result.ready
    assert [f.label for f in result.blanks] == ["How many years of experience do you have with Python?"]


@pytest.mark.parametrize(
    ("name", "error", "message"),
    [
        ("linkedin_external.html", external.ExternalApplyError, r"company's own site \(acme\.example\)"),
        ("linkedin_limit.html", linkedin.LimitReachedError, "daily Easy Apply limit"),
    ],
)
async def test_easy_apply_refusals(page: Any, name: str, error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        await linkedin.open_easy_apply(page, url(name))


async def test_naukri_one_click_apply(page: Any) -> None:
    await naukri.open_job(page, url("naukri.html"))
    await page.locator(naukri.APPLY_BUTTON).first.click()
    assert await confirmed(page, naukri.NAUKRI)


async def test_naukri_hands_over_company_site_jobs_and_chatbot_questions(
    page: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(external, "FOLLOW_WAIT_S", 1.0)
    # "Apply on company site" that leads nowhere Sarthi can read (the working one: test_external.py).
    with pytest.raises(FillError, match="company's own site, and its link could not be read"):
        await naukri.open_job(page, url("naukri_company_dead.html"))
    await naukri.open_job(page, url("naukri_chatbot.html"))
    await page.locator(naukri.APPLY_BUTTON).first.click()
    assert await naukri.chatbot_open(page)
    assert not await confirmed(page, naukri.NAUKRI)


@pytest.mark.parametrize(
    ("given", "li", "nk"),
    [
        ("https://www.linkedin.com/jobs/view/1", True, False),
        ("https://in.linkedin.com/jobs/view/1", True, False),
        ("https://www.naukri.com/job-listings-x-1", False, True),
        ("https://linkedin.com.evil.example/jobs/1", False, False),
        ("https://notnaukri.com.example/x", False, False),
    ],
)
def test_board_detection(given: str, li: bool, nk: bool) -> None:
    assert linkedin.is_linkedin(given) is li
    assert naukri.is_naukri(given) is nk
