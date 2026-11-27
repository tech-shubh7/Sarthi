"""The sidebar: each section with its own pages beneath it, the current page marked, counts as badges."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sarthi.db.application_dashboard import ApplicationView
from sarthi.db.inbox import InboxCounts
from sarthi.web.application_view import LISTED
from sarthi.web.sections import ACTIVITY_SECTIONS, SETTINGS_SECTIONS, SOURCE_SECTIONS


@dataclass(frozen=True, slots=True)
class NavLink:
    """A page in the sidebar."""

    key: str
    title: str
    href: str
    count: int = 0
    hot: bool = False  # the count is work waiting for the user
    current: bool = False


@dataclass(frozen=True, slots=True)
class NavSection:
    """A top-level section; ``pages`` are its own pages (none: the section is one page)."""

    key: str
    title: str
    href: str
    icon: str
    count: int = 0
    hot: bool = False
    current: bool = False
    pages: tuple[NavLink, ...] = ()
    always_open: bool = False  # its pages show even while another section is open

    @property
    def open(self) -> bool:
        """Whether its pages show in the sidebar."""
        return bool(self.pages) and (self.current or self.always_open)

    @property
    def current_page(self) -> NavLink | None:
        """The page of this section being shown."""
        return next((p for p in self.pages if p.current), None)


def _links(
    prefix: str, sections: Mapping[str, str], current: str, counts: Mapping[str, int]
) -> tuple[NavLink, ...]:
    return tuple(
        NavLink(key, title, f"{prefix}/{key}", counts.get(key, 0), bool(counts.get(key)), key == current)
        for key, title in sections.items()
    )


def sidebar(
    section: str, page: str, counts: Mapping[ApplicationView, int], inbox: InboxCounts
) -> tuple[NavSection, ...]:
    """Every section, with ``section``/``page`` marked current."""
    applications = tuple(
        NavLink(
            p.slug, p.title, p.href, counts.get(p.view, 0), p.blocked and bool(counts.get(p.view)),
            section == "applications" and p.slug == page,
        )
        for p in LISTED
    )  # fmt: skip
    blocked = sum(link.count for link in applications if link.hot)
    activity = {"questions": inbox.questions, "auto-apply": inbox.auto}
    return (
        NavSection("overview", "Overview", "/", "overview", current=section == "overview"),
        NavSection(
            "matches", "Matches", "/matches", "matches", inbox.strong, bool(inbox.strong),
            section == "matches",
        ),
        NavSection(
            "applications", "Applications", "/applications", "applications", blocked, bool(blocked),
            section == "applications", applications, always_open=True,
        ),
        NavSection(
            "activity", "Activity", "/activity/timeline", "check", inbox.questions + inbox.auto,
            bool(inbox.questions + inbox.auto), section == "activity",
            _links("/activity", ACTIVITY_SECTIONS, page if section == "activity" else "", activity),
        ),
        NavSection(
            "sources", "Sources", "/sources/readers", "sources", current=section == "sources",
            pages=_links("/sources", SOURCE_SECTIONS, page if section == "sources" else "", {}),
        ),
        NavSection(
            "settings", "Settings", "/settings/profile", "settings", current=section == "settings",
            pages=_links("/settings", SETTINGS_SECTIONS, page if section == "settings" else "", {}),
        ),
    )  # fmt: skip


def crumbs(sections: tuple[NavSection, ...], title: str = "") -> list[tuple[str, str]]:
    """Section, then page (then ``title`` when the page is not in the sidebar): (name, address)."""
    current = next((s for s in sections if s.current), None)
    if current is None or current.key == "overview":
        return []
    trail = [(current.title, current.href)]
    if current.current_page is not None:
        trail.append((current.current_page.title, current.current_page.href))
    elif title:
        trail.append((title, ""))
    return trail
