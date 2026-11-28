"""The browser extension's own page scripts (scan.js as packaged, fill.js) in a real Chromium."""

import base64
import io
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from sarthi.apply.adapters import GENERIC
from sarthi.apply.engine import ApplyEngine
from sarthi.extension import ExtensionService
from sarthi.jobs.forms import Applicant, FormContext, FormField, suggest

FORMS = Path(__file__).parent / "forms"
PACKAGE = zipfile.ZipFile(io.BytesIO(ExtensionService.package()))
PDF = b"%PDF-1.4 tailored resume"


@pytest.fixture
async def page(tmp_path: Path) -> AsyncIterator[Any]:
    engine = ApplyEngine(tmp_path / "shots", url_check=None)
    try:
        made = await engine._new_page()
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"Chromium not installed for Playwright: {error}")
    await made.goto((FORMS / "extension_form.html").as_uri())
    for name in ("scan.js", "fill.js"):  # exactly the files the extension injects
        await made.add_script_tag(content=PACKAGE.read(name).decode())
    yield made
    await engine.aclose()


def answers_for(fields: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """What Sarthi's /ext/fill answers for these fields (the same suggest() rules)."""
    me = Applicant(first_name="Asha", email="asha@example.com", work_authorized=True, years_experience=2,
                   skills=("Python",))  # fmt: skip
    ctx = FormContext(me, resume_file="/data/r.pdf", job_location="Pune, India")
    out = {}
    for raw in fields:
        field = FormField(raw["key"], raw["label"], raw["type"], raw["required"], tuple(raw["options"]))
        if (hit := suggest(field, ctx)) is not None:
            out[field.key] = {"value": "resume" if hit.source == "file" else hit.value, "source": hit.source}
    return out


async def test_the_extension_fills_the_form_and_never_submits(page: Any) -> None:
    fields = await page.evaluate("cfg => self.__sarthiScan(cfg)", GENERIC.scan_config())
    labels = {f["label"]: f for f in fields}
    assert "Create a password" in labels and labels["Gender"]["type"] == "radio"
    answers = answers_for(fields)
    answers[labels["Create a password"]["key"]] = {"value": "hunter2", "source": "saved"}  # even if told to
    files = {"resume": {"name": "Asha_Rao_Acme_Resume.pdf", "data": base64.b64encode(PDF).decode()}}
    report = await page.evaluate("([a, f]) => self.__sarthiFill(a, f)", [answers, files])
    assert report["attached"] == ["Asha_Rao_Acme_Resume.pdf"]
    assert await page.input_value("#first") == "Asha"
    assert await page.input_value("#years") == "2"  # from the profile: Python is on the resume
    assert await page.input_value("#auth") == "1"  # the option reading "Yes"
    assert await page.is_checked("input[value=d]")  # gender: declined
    assert await page.input_value("#email") == "already@example.com"  # what the site put there is kept
    assert labels["Email"]["key"] in report["kept"]
    assert await page.input_value("#pw") == ""  # never a password
    assert await page.input_value("#why") == ""  # only the user answers this
    state = await page.evaluate("window.state")
    assert state["first"] == "Asha" and state["years"] == "2"  # a React-style form saw the values
    attached = await page.evaluate(
        "() => { const f = document.querySelector('#resume').files[0]; return [f.name, f.type, f.size]; }"
    )
    assert attached == ["Asha_Rao_Acme_Resume.pdf", "application/pdf", len(PDF)]
    assert await page.evaluate("document.body.dataset.sent") is None  # nothing was submitted


async def test_after_submit_the_users_own_answers_are_read_but_never_a_password(page: Any) -> None:
    fields = await page.evaluate("cfg => self.__sarthiScan(cfg)", GENERIC.scan_config())
    key = {f["label"]: f["key"] for f in fields}
    await page.fill("#why", "I like your payments work.")
    await page.fill("#pw", "hunter2")
    wanted = [{"key": key["Why do you want to work here?"], "label": "Why do you want to work here?"},
              {"key": key["Create a password"], "label": "Create a password"}]  # fmt: skip
    read = await page.evaluate("w => self.__sarthiRead(w)", wanted)
    assert read == [{"label": "Why do you want to work here?", "value": "I like your payments work."}]
