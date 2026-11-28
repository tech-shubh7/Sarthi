"""Freehire's apply links are redirects: resolved to the employer's page by one request, never a browser."""

import httpx
import pytest

from sarthi.apply.redirects import is_redirect_link, resolve

GO = "https://freehire.me/api/v1/go/software-engineer-i-backend-kirana-club-meesho-shydsvir?src=web"
LEVER = "https://jobs.lever.co/acme/73db1822?utm_source=freehire.me&lever-origin=applied"


def test_only_freehire_go_links_are_redirects() -> None:
    assert is_redirect_link(GO)
    assert not is_redirect_link("https://freehire.me/jobs/123")
    assert not is_redirect_link("https://jobs.lever.co/acme/1")


async def test_a_go_link_resolves_to_the_employer_page_without_tracking() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": LEVER})

    real = await resolve(GO, transport=httpx.MockTransport(handler))
    assert real == "https://jobs.lever.co/acme/73db1822?lever-origin=applied"
    assert seen == [GO]  # one request, and the employer's page itself is never fetched


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="<html>blocked</html>"),  # no redirect: keep the link as it was
        httpx.Response(302, headers={"location": "javascript:alert(1)"}),  # never a non-web address
        httpx.Response(302, headers={"location": GO}),  # a loop back to itself
    ],
)
async def test_a_link_that_does_not_lead_anywhere_usable_is_kept(answer: httpx.Response) -> None:
    assert await resolve(GO, transport=httpx.MockTransport(lambda request: answer)) is None


async def test_a_network_failure_keeps_the_link() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert await resolve(GO, transport=httpx.MockTransport(fail)) is None
