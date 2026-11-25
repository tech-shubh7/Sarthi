"""The apply engine in a real headless browser, against local test forms (no network)."""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sarthi.apply import adapters
from sarthi.apply.adapters import THANKS, PostingClosedError, adapter_for, apply_url, is_workday
from sarthi.apply.engine import ApplyEngine, EnginePausedError, FillError
from sarthi.jobs.forms import Applicant, FormContext

FORMS = Path(__file__).parent / "forms"
ASHA = Applicant(
    first_name="Asha", last_name="Rao", email="asha@example.com", country="India", work_authorized=True
)


def url(name: str) -> str:
    return (FORMS / name).as_uri()


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[ApplyEngine]:
    made = ApplyEngine(tmp_path / "shots", url_check=None)  # local test pages
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


async def test_no_confirmation_after_the_press_is_unknown_not_a_success_or_a_retry(
    engine: ApplyEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sarthi.apply.engine.SUBMIT_WAIT_S", 2.0)
    result = await engine.fill("job-5", url("broken.html"), FormContext(ASHA))
    outcome = await engine.submit("job-5", result.code)
    assert outcome.outcome == "unknown"  # pressed: it may have gone out
    assert "check the site" in outcome.detail.lower()


async def test_a_page_without_an_application_form_is_never_offered_for_submit(engine: ApplyEngine) -> None:
    with pytest.raises(FillError, match="no application form"):
        await engine.fill("job-np", url("job_page_only.html"), FormContext(ASHA))
    assert not engine.can_submit("job-np", "ANY123")
    assert "job-np" not in engine._open
    with pytest.raises(FillError, match="no application form"):  # a careers site's search box only
        await engine.fill("job-ns", url("careers_search.html"), FormContext(ASHA))
    assert "job-ns" not in engine._open


async def test_a_consent_banner_over_submit_is_dismissed_with_its_least_sharing_choice(
    engine: ApplyEngine,
) -> None:
    result = await engine.fill("job-c1", url("submit_consent.html"), FormContext(ASHA))
    page = engine._open["job-c1"].page
    done = await engine.submit("job-c1", result.code)
    assert done.outcome == "submitted", done.detail
    assert page.url.endswith("thanks.html#reject")  # the banner's "Reject all", then the real Submit


async def test_a_hidden_twin_of_submit_is_skipped_for_the_visible_one(engine: ApplyEngine) -> None:
    # A faded sticky bar's "Submit" and a mobile copy come first in the page; clicking the first match waited
    # 15 s on the invisible bar ("Locator.click: Timeout 15000ms exceeded").
    result = await engine.fill("job-c2", url("submit_duplicate.html"), FormContext(ASHA))
    page = engine._open["job-c2"].page
    done = await engine.submit("job-c2", result.code)
    assert done.outcome == "submitted", done.detail
    assert page.url.endswith("thanks.html#form")  # the form's own button, not a hidden copy


async def test_a_submit_disabled_for_a_moment_is_pressed_once_enabled(engine: ApplyEngine) -> None:
    result = await engine.fill("job-c3", url("submit_disabled.html"), FormContext(ASHA))
    done = await engine.submit("job-c3", result.code)
    assert done.outcome == "submitted", done.detail


async def test_a_submit_that_stays_disabled_goes_to_the_user(
    engine: ApplyEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sarthi.apply.adapters.SUBMIT_FIND_S", 1.0)
    result = await engine.fill("job-c4", url("submit_disabled.html") + "?never", FormContext(ASHA))
    done = await engine.submit("job-c4", result.code)
    assert done.outcome == "needs_human" and "disabled" in done.detail
    assert "Timeout" not in done.detail
    assert done.screenshot is not None and done.screenshot.is_file()


async def test_something_else_over_submit_is_never_clicked_through(
    engine: ApplyEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sarthi.apply.adapters.SUBMIT_CLICK_MS", 500)
    result = await engine.fill("job-c5", url("submit_covered.html"), FormContext(ASHA))
    page = engine._open["job-c5"].page
    done = await engine.submit("job-c5", result.code)
    assert done.outcome == "needs_human", done.detail
    assert "Complete your profile" in done.detail and "Timeout" not in done.detail
    assert "Thank you" not in await page.inner_text("body")  # not sent behind the user's back


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


async def test_a_closed_posting_is_never_offered_for_submit(engine: ApplyEngine) -> None:
    with pytest.raises(PostingClosedError, match="position has been filled"):
        await engine.fill("job-closed", url("closed.html"), FormContext(ASHA))
    assert "job-closed" not in engine._open


async def test_a_known_hiring_system_page_without_a_form_is_never_offered_for_submit(
    engine: ApplyEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Greenhouse/Lever/Ashby: ``prepare`` waits for a form that never comes and discover finds nothing.
    monkeypatch.setattr("sarthi.apply.adapters.HYDRATE_TIMEOUT_MS", 500)
    page = await engine._new_page()
    await page.goto(url("empty.html"))
    with pytest.raises(FillError, match="no application form on the page"):
        await engine._fill_with("job-gh", page, adapters.LEVER, FormContext(ASHA))
    assert "job-gh" not in engine._open


async def test_a_removed_posting_404_is_closed(engine: ApplyEngine, local_site: str) -> None:
    with pytest.raises(PostingClosedError, match="404"):
        await engine.fill("job-404", f"{local_site}/gone", FormContext(ASHA))


async def test_links_to_non_public_addresses_are_never_opened(tmp_path: Path) -> None:
    guarded = ApplyEngine(tmp_path / "shots")  # the production default: public addresses only
    for link in ("http://127.0.0.1:9/apply", "http://192.168.1.1/admin", "file:///etc/passwd"):
        with pytest.raises(FillError, match="not a public web address"):
            await guarded.fill("job-ssrf", link, FormContext(ASHA))
    assert guarded._context is None  # no browser was even started
    await guarded.aclose()


async def test_the_route_guard_blocks_private_hosts_and_redirects_to_them(
    tmp_path: Path, local_site: str
) -> None:
    async def only_127(link: str) -> bool:  # stands in for "public": 127.0.0.1 allowed, localhost not
        return adapters.host_of(link) == "127.0.0.1"

    guarded = ApplyEngine(tmp_path / "shots", url_check=only_127)
    try:
        page = await guarded._new_page()
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"Chromium not installed for Playwright: {error}")
    port = local_site.rsplit(":", 1)[1]
    await page.goto(f"{local_site}/page")
    await page.evaluate(f"fetch('http://localhost:{port}/direct').catch(() => null)")
    await page.wait_for_timeout(300)
    assert "/direct" not in HITS  # a request the page makes to a private host never leaves the browser
    with pytest.raises(FillError, match="not a public web address"):  # nor is a redirect's page used
        await guarded.fill("job-r", f"{local_site}/redirect", FormContext(ASHA))
    assert "job-r" not in guarded._open
    await guarded.aclose()


def test_a_footer_thanks_for_interest_is_not_a_submission() -> None:
    assert not THANKS.search("Careers | Thank you for your interest in Example Corp | Privacy")
    assert THANKS.search("Thank you for applying!")


async def test_new_thanks_beside_a_form_still_on_screen_is_not_sent(engine: ApplyEngine) -> None:
    await engine.fill("job-t", url("captcha.html"), FormContext(ASHA, answers={"Full name": "Asha Rao"}))
    page = engine._open["job-t"].page
    await page.evaluate(
        "document.body.insertAdjacentHTML('beforeend', '<p>Application submitted by 1,000 people</p>')"
    )
    done = await engine.watch("job-t", wait_s=2)
    assert done.outcome == "needs_human", done.detail


async def test_a_visible_window_pauses_the_engine_without_closing_it_under_a_fill(tmp_path: Path) -> None:
    paused_engine = ApplyEngine(tmp_path / "shots", url_check=None)
    order: list[str] = []
    async with paused_engine._in_use():  # a fill under way

        async def window() -> None:
            async with paused_engine.paused():
                order.append("window")
                with pytest.raises(EnginePausedError):  # new fills wait for the window
                    await paused_engine.fill("job-p", url("basic.html"), FormContext(ASHA))

        opening = asyncio.create_task(window())
        await asyncio.sleep(0.05)
        assert order == []  # the window waits for the running fill
        order.append("fill done")
    await opening
    assert order == ["fill done", "window"]
    assert not paused_engine._held


HITS: list[str] = []


@pytest.fixture
def local_site() -> Iterator[str]:
    class Site(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            HITS.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header(
                    "Location", f"http://localhost:{str(self.headers['Host']).rsplit(':', 1)[1]}/secret"
                )
                self.end_headers()
                return
            self.send_response(404 if self.path == "/gone" else 200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>Not found</body></html>")

        def log_message(self, *args: object) -> None:
            pass

    HITS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
