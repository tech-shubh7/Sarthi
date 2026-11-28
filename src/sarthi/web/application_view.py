"""The Applications pages: one page per state, their addresses, and what each page shows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sarthi.db.application_dashboard import ApplicationItem, ApplicationSort, ApplicationView
from sarthi.db.inbox import InboxCounts


@dataclass(frozen=True, slots=True)
class ApplicationPage:
    """One list page: its address, its state, and what the user does there."""

    slug: str
    view: ApplicationView
    title: str
    lede: str
    blocked: bool  # waiting for the user: Sarthi can go no further alone
    todo: str = ""  # the blocker said as a task (overview "Needs you")
    empty: str = "Nothing here right now."

    @property
    def replies(self) -> bool:
        """Whether its rows can have an employer's reply (only sent ones can)."""
        return self.view in {ApplicationView.SUBMITTED, ApplicationView.ALL}

    @property
    def href(self) -> str:
        """The page's address."""
        return f"/applications/{self.slug}"


PAGES: tuple[ApplicationPage, ...] = (
    ApplicationPage(
        "needs-you", ApplicationView.INPUT, "Needs you",
        "Answer the employer's questions, or check a form Sarthi filled and press Submit.", True,
        "Questions to answer or forms to approve", "No questions or filled forms are waiting for you.",
    ),
    ApplicationPage(
        "captcha", ApplicationView.CAPTCHA, "Solve CAPTCHA",
        "These forms stopped at a CAPTCHA. Press Finish it myself: the filled form opens on this computer, "
        "you solve the CAPTCHA and press Submit there. Sarthi never solves CAPTCHAs.", True,
        "CAPTCHA to solve", "No form is waiting at a CAPTCHA.",
    ),
    ApplicationPage(
        "login-needed", ApplicationView.LOGIN, "Login needed",
        "These sites showed you as signed out. Log in once per site: Sarthi keeps the site's session "
        "(never your password).", True,
        "Job sites to log in to", "No site is waiting for a login.",
    ),
    ApplicationPage(
        "apply-yourself", ApplicationView.MANUAL, "Apply yourself",
        "Sarthi cannot send these (a Workday account, a site that refuses automated browsers, a form it "
        "could not read). Open the job, apply on the site, then press I applied. The Sarthi browser "
        "extension fills these forms for you (Settings > Browser extension).", True,
        "Applications to send yourself", "Nothing to apply to by hand.",
    ),
    ApplicationPage(
        "verify", ApplicationView.VERIFY, "Verify submission",
        "Submit was pressed but no confirmation appeared. Check the site or your email before trying "
        "again.", True,
        "Submits to check", "No submit has an unknown result.",
    ),
    ApplicationPage(
        "in-progress", ApplicationView.QUEUED, "In progress",
        "Sarthi is writing documents, reading or filling these forms. Nothing to do.", False,
        "Documents or forms being prepared", "Nothing is being worked on right now.",
    ),
    ApplicationPage(
        "submitted", ApplicationView.SUBMITTED, "Submitted",
        "Applications that were sent, by Sarthi or by you, with the employer's latest reply.", False,
        "Sent by Sarthi or by you", "No application has been sent yet.",
    ),
    ApplicationPage(
        "closed", ApplicationView.STOPPED, "Closed or stopped",
        "Postings that closed, and applications that stopped for good.", False,
        "Closed postings, stopped for good", "Nothing closed or stopped.",
    ),
    ApplicationPage(
        "all", ApplicationView.ALL, "All applications",
        "Every application in every state: search across all of them.", False,
        empty="Applications appear here once Sarthi queues them or records a result.",
    ),
)  # fmt: skip
BY_SLUG = {page.slug: page for page in PAGES}
BY_VIEW = {page.view: page for page in PAGES}
LISTED = tuple(page for page in PAGES if page.view is not ApplicationView.ALL)  # in the sidebar
# Addresses from before each state had its own page: still work, and lead to the page now.
OLD_SLUGS = {
    ApplicationView.INPUT.value: "needs-you",
    ApplicationView.MANUAL.value: "apply-yourself",
    ApplicationView.QUEUED.value: "in-progress",
    ApplicationView.STOPPED.value: "closed",
    ApplicationView.LOGIN.value: "login-needed",
}

APPLICATION_SORTS = {
    ApplicationSort.NEWEST: "Recently updated",
    ApplicationSort.OLDEST: "Oldest first",
    ApplicationSort.COMPANY: "Company A-Z",
    ApplicationSort.ROLE: "Role A-Z",
}


def application_sort(value: str) -> ApplicationSort:
    """Use the default sort for an unknown order."""
    try:
        return ApplicationSort(value)
    except ValueError:
        return ApplicationSort.NEWEST


def page_for(name: str) -> ApplicationPage | None:
    """The page for a slug, an old slug or a state name (``?show=input``); None when there is none."""
    slug = OLD_SLUGS.get(name, name)
    return BY_SLUG.get(slug) or next((p for p in PAGES if p.view.value == name), None)


@dataclass(frozen=True, slots=True)
class Blocker:
    """One kind of thing waiting for the user, with how many and where to do it."""

    title: str
    count: int
    href: str


def blockers(counts: Mapping[ApplicationView, int], inbox: InboxCounts) -> list[Blocker]:
    """What waits for the user, in the order to do it (zero counts kept: the page says "nothing")."""
    found = [Blocker(page.todo, counts.get(page.view, 0), page.href) for page in PAGES if page.blocked]
    found.append(Blocker("Auto-applies you can still cancel", inbox.auto, "/activity/auto-apply"))
    return found


@dataclass(frozen=True, slots=True)
class SummaryCard:
    """One state on the Applications summary."""

    page: ApplicationPage
    count: int


def summary(counts: Mapping[ApplicationView, int]) -> tuple[list[SummaryCard], list[SummaryCard]]:
    """(waiting for you, moving along): a card per state with its count."""
    cards = [SummaryCard(page, counts.get(page.view, 0)) for page in LISTED]
    return [c for c in cards if c.page.blocked], [c for c in cards if not c.page.blocked]


@dataclass(frozen=True, slots=True)
class LoginGroup:
    """The jobs of one site that wait for a login there."""

    site: str
    count: int  # all of the site's waiting jobs (not only this page's)
    login: tuple[str, str] | None  # (key, name) of the dashboard's login for the site; None: none offered
    rows: list[ApplicationItem]


def login_for(host: str, sites: Sequence[tuple[str, str]]) -> tuple[str, str] | None:
    """The offered login whose name is one of the host's labels (``in.indeed.com`` -> indeed)."""
    labels = set(host.lower().split("."))
    return next(((key, name) for key, name in sites if key in labels), None)


def login_groups(
    rows: Sequence[ApplicationItem], per_site: Mapping[str, int], sites: Sequence[tuple[str, str]]
) -> list[LoginGroup]:
    """This page's rows by site (they arrive sorted by site), each with the site's login."""
    groups: dict[str, LoginGroup] = {}
    for row in rows:
        group = groups.get(row.site)
        if group is None:
            group = groups[row.site] = LoginGroup(
                row.site, per_site.get(row.site, 0), login_for(row.site, sites), []
            )
        group.rows.append(row)
    return list(groups.values())
