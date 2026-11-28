"""Applications filling at the same time never take (or close) each other's tabs (2026-11-28)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from sarthi.apply import adapters
from sarthi.apply.engine import CLOSED_RETRIES, CLOSED_UNDER_FILL, ApplyEngine, EnginePausedError

OPENER = "<button onclick=\"window.open('about:blank','_blank','noopener')\">Apply</button>"


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[ApplyEngine]:
    made = ApplyEngine(tmp_path / "shots", url_check=None)
    try:
        page = await made._new_page()
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"Chromium not installed for Playwright: {error}")
    await page.close()
    yield made
    await made.aclose()


async def test_only_the_pages_own_tab_is_seen(engine: ApplyEngine) -> None:
    mine: Any = await engine._new_page()
    other: Any = await engine._new_page()  # another application's form, already open
    await mine.set_content(OPENER)
    with adapters.popups(mine) as opened:
        late_other = await engine._new_page()  # opened by someone else while this one waits
        await mine.locator("button").click()
        await mine.wait_for_timeout(500)
    assert len(opened) == 1 and opened[0] not in (other, late_other)  # a noopener tab is still caught
    assert not other.is_closed() and not late_other.is_closed()


async def test_a_browser_closed_under_a_fill_is_filled_again_twice_at_most(engine: ApplyEngine) -> None:
    gone = RuntimeError("Page.evaluate: Target page, context or browser has been closed")
    for _ in range(CLOSED_RETRIES):
        with pytest.raises(EnginePausedError, match=CLOSED_UNDER_FILL):
            async with engine._interruptible("job-1"):
                raise gone
    with pytest.raises(RuntimeError):  # a site that closes its own tab every time reaches the user
        async with engine._interruptible("job-1"):
            raise gone
