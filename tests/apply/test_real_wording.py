"""Wording seen on the real boards (2026-11-26) is read the right way."""

import pytest

from sarthi.apply import adapters, naukri


@pytest.mark.parametrize(
    "text",
    [
        "Not currently accepting applications",  # LinkedIn, a closed posting while signed in
        "No longer accepting applications",
        "This job has expired",
    ],
)
def test_closed_postings_are_recognised(text: str) -> None:
    assert adapters.CLOSED_TEXT.search(text)


@pytest.mark.parametrize("text", ["Accepting applications", "Apply now: actively reviewing applicants"])
def test_open_postings_are_not_taken_for_closed(text: str) -> None:
    assert not adapters.CLOSED_TEXT.search(text)


def test_naukris_firewall_page_is_recognised() -> None:
    page = (
        'Access Denied\nYou don\'t have permission to access "http://www.naukri.com/job-listings-x" on this '
        "server.\nReference #18.aef5d217"
    )
    assert naukri._BLOCKED.search(page)
    assert not naukri._BLOCKED.search("Access denied to the admin panel is logged. Apply now")
