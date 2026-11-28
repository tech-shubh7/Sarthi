"""The dashboard's web app: overview, matches, applications, sources and settings.

It runs on this computer only. A browser sends requests to 127.0.0.1 from any site you have open, so:
requests naming any other host are refused (a site posing as localhost, "DNS rebinding"); every change is a
form posted from these pages, carrying a secret made when Sarthi started and an Origin of this computer (a
page elsewhere cannot forge either); pages may not be framed or load anything from elsewhere. Reads use their
own read-only connection; changes go through the running app (``DashboardActions``), never straight to SQL.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware

from sarthi import __version__
from sarthi.clock import Clock, utcnow
from sarthi.db.application_dashboard import ApplicationDashboard, ApplicationFilters, ApplicationView
from sarthi.db.dashboard import DashboardQueries
from sarthi.db.documents import DocumentStore
from sarthi.db.inbox import InboxQueries
from sarthi.db.notices import NoticeStore
from sarthi.web.actions import DashboardActions
from sarthi.web.application_view import (
    APPLICATION_SORTS,
    BY_VIEW,
    PAGES,
    ApplicationPage,
    application_sort,
    blockers,
    login_groups,
    page_for,
    summary,
)
from sarthi.web.apply_kit import apply_kit
from sarthi.web.mail_form import PRESETS, mail_from_form, mail_values
from sarthi.web.navigation import crumbs, sidebar
from sarthi.web.sections import (
    ACTIVITY_SECTIONS,
    MATCH_SORTS,
    SETTINGS_SECTIONS,
    SOURCE_SECTIONS,
    TIMELINE_SORTS,
    notice_kind,
)
from sarthi.web.settings_form import REMOTE_CHOICES, form_values, profile_from_form
from sarthi.web.sources_view import SiteMap, sources_view

HERE = Path(__file__).parent
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
LOCAL_NAMES = {"127.0.0.1", "localhost", "::1"}
MATCH_DAYS = 7
DECISIONS = {"alert": ("alert",), "digest": ("digest",), "all": ("alert", "digest")}
MINUTES_PER_HOUR, HOURS_SHOWN = 60, 48
MAX_RESUME_BYTES = 5_000_000
PAGE_SIZE = 25
JOB_ACTIONS = ("apply", "skip", "later")
BACK_TO = {"/matches", "/", "/applications", "/settings/logins", *(p.href for p in PAGES)}
KEPT_PARAMS = ("show", "q", "page", "site", "sort")  # what a page's address may carry back after a change
ANSWER = "a."  # a question's answer field on the Activity page: "a." + the form field's key
NOTICES_SHOWN = 50
# What a change did, by a fixed key in the address: nothing a page elsewhere puts in a link is ever shown.
DONE = {
    "profile": "Saved. Recent jobs are being matched again with your new profile.",
    "alerts": "Saved.",
    "mail": "Mail settings saved. They count from the next check: no restart needed.",
    "apply": "Apply started: the tailored resume comes first, then the filled form waits for your Submit.",
    "skip": "Skipped.",
    "later": "Saved for later.",
    "submitted": "Submitting. The result shows here (and in Telegram, if connected).",
    "cancelled": "Cancelled: nothing was sent.",
    "gone": "That is no longer waiting.",
    "applied": "Marked as applied. Good luck!",
    "refill": "Filling the form again. It shows here for your Submit in a minute or two.",
    "finish": "Opening the form, filled, on this computer (in Docker its link shows at the top of this "
    "page). Solve the CAPTCHA and press Submit there: Sarthi sees it and records it.",
    "busy": "A browser window is already open (a login or another form). Finish that one first.",
    "answered": "Saved. When every question is answered, Sarthi fills the form and it waits for your Submit.",
    "declined": "OK, Sarthi won't apply to that one.",
    "auto_cancelled": "Auto-apply cancelled. You can still tap Apply on the job.",
    "auto_now": "Applying now.",
    "matching_queued": "Eligible matches are queued. Follow their progress in Applications.",
    "matching_manual": "These applications need to be submitted on the employer's site.",
    "matching_empty": "No new eligible matches are ready to queue at your current threshold.",
    "login": "Opening the login page in a window on this computer (in Docker its link shows at the top of "
    "this page). Log in, then close the window; Sarthi checks the login. It never sees your password.",
}


def asset(path: str) -> str:
    """'/static/app.css?v=3f9c1a2b7d': a new version of the file gets a new address."""
    digest = hashlib.sha256((HERE / "static" / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


def ago(iso: str | None, now: datetime) -> str:
    """'just now', '12 min ago', '3 h ago', '2 days ago', or ''."""
    if not iso:
        return ""
    try:
        at = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    at = at if at.tzinfo else at.replace(tzinfo=UTC)
    minutes = max(int((now - at).total_seconds() // 60), 0)
    if minutes < 1:
        return "just now"
    if minutes < MINUTES_PER_HOUR:
        return f"{minutes} min ago"
    if minutes < HOURS_SHOWN * MINUTES_PER_HOUR:
        return f"{minutes // MINUTES_PER_HOUR} h ago"
    return f"{minutes // (24 * MINUTES_PER_HOUR)} days ago"


def read_only(db_path: Path) -> sqlite3.Connection:
    """A connection that cannot change anything (pages only read; changes go through the app)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _from_here(request: Request) -> bool:
    """True when the browser says the form came from a page on this computer (Origin, else Referer)."""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    try:
        host = (urlparse(source).hostname or "").lower()
    except ValueError:
        return False
    return host in LOCAL_NAMES


def _page(asked: int, total: int) -> tuple[int, int]:
    """(this page, number of pages): an out-of-range page number shows the nearest real page."""
    pages = max(1, -(-total // PAGE_SIZE))
    return min(max(asked, 1), pages), pages


def _own_page(target: str, default: str = "/matches") -> str:
    """``target`` when it is one of the dashboard's own pages (path and query only); else ``default``."""
    parts = urlparse(target)
    if parts.scheme or parts.netloc or parts.path not in BACK_TO:
        return default  # never a redirect to another site, whatever a form says
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _text_fields(form: FormData) -> dict[str, str]:
    return {key: value for key, value in form.items() if isinstance(value, str)}


def create_app(  # noqa: PLR0915  (one route per page and action)
    conn: sqlite3.Connection,
    clock: Clock = utcnow,
    *,
    actions: DashboardActions | None = None,
    data_dir: Path | None = None,
    sites: SiteMap | None = None,
) -> FastAPI:
    """The dashboard over ``conn`` (open it with ``read_only``); ``actions`` None: nothing can be changed.

    ``sites`` says which source reads which site (built by the composition root from the sources).
    """
    queries = DashboardQueries(conn)
    inbox, notices = InboxQueries(conn), NoticeStore(conn)
    application_board = ApplicationDashboard(conn)
    site_map = sites or SiteMap()
    token = secrets.token_urlsafe(32)  # new each start: a form from an older tab must be loaded again
    app = FastAPI(title="Sarthi", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    @app.middleware("http")
    async def _headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers.update(HEADERS)
        static = request.url.path.startswith("/static/")
        response.headers["Cache-Control"] = "no-cache" if static else "no-store"
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(asset=asset, version=__version__, notice_kind=notice_kind)

    def page(  # noqa: PLR0913 (the page, where it sits in the sidebar, and its own values)
        request: Request,
        name: str,
        nav: str,
        sub: str = "",
        *,
        status: int = 200,
        crumb: str = "",
        **context: Any,
    ) -> HTMLResponse:
        """``name`` rendered as page ``sub`` of sidebar section ``nav`` (``crumb``: a page not in it)."""
        now = clock()
        start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        overview = queries.overview(start)
        inbox_counts = inbox.counts(now - timedelta(days=MATCH_DAYS))
        counts = application_board.counts()
        sections = sidebar(nav, sub, counts, inbox_counts)
        done = DONE.get(request.query_params.get("done", ""), "")
        context.setdefault("screen", actions.screen_link() if actions is not None else None)
        return templates.TemplateResponse(
            request,
            name,
            {
                "nav": nav,
                "sections": sections,
                "current_section": next((s for s in sections if s.current), None),
                "crumbs": crumbs(sections, crumb),
                "blockers": blockers(counts, inbox_counts),
                "application_counts": counts,
                "ov": overview,
                "inbox": inbox_counts,
                "ago": lambda iso: ago(iso, now),
                "csrf": token,
                "can_edit": actions is not None,
                "done": done,
                # The page's own address: a form posted from it brings the user back to the same tab and page.
                "here": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
                **context,
            },
            status_code=status,
        )

    async def checked_form(request: Request) -> tuple[DashboardActions, FormData]:
        """The posted form, after the checks every change must pass."""
        if actions is None:
            raise HTTPException(403, "This dashboard only reads: start Sarthi (sarthi run) to change things.")
        if not _from_here(request):
            raise HTTPException(403, "Changes are accepted only from the dashboard's own pages.")
        form = await request.form(max_files=1, max_fields=40)
        if not hmac.compare_digest(str(form.get("csrf", "")), token):
            raise HTTPException(403, "This page is out of date: reload it and try again.")
        return actions, form

    def back(path: str, done: str, anchor: str = "") -> RedirectResponse:
        """Back to the page the form was on (its tab, page and search kept), scrolled to the same row."""
        target = urlparse(path)
        query = [(k, v) for k, v in parse_qsl(target.query) if k in KEPT_PARAMS]
        url = target.path + "?" + urlencode([*query, ("done", done)])
        return RedirectResponse(url + (f"#{anchor}" if anchor else ""), status_code=303)

    # ---- pages ----

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> HTMLResponse:
        # A first start: what is still missing before any job can be matched (shown until both are done).
        start = (
            {"profile": actions.profile() is not None, "ai": actions.ai_ready()}
            if actions is not None
            else None
        )
        return page(request, "overview.html", "overview", start=start)

    @app.get("/matches", response_class=HTMLResponse)
    async def matches(
        request: Request,
        show: str = "all",
        q: str = "",
        sort: str = "score",
        page_no: int = Query(1, alias="page"),
    ) -> HTMLResponse:
        show = show if show in DECISIONS else "all"
        sort = sort if sort in MATCH_SORTS else "score"
        q = q.strip()[:80]
        since = clock() - timedelta(days=MATCH_DAYS)
        counts = {key: queries.count_matches(kinds, since, q) for key, kinds in DECISIONS.items()}
        total = counts[show]
        number, pages = _page(page_no, total)
        rows = queries.matches(
            DECISIONS[show], since, PAGE_SIZE, offset=(number - 1) * PAGE_SIZE, query=q, sort=sort
        )

        def link(show: str = show, page: int = number) -> str:
            return "/matches?" + urlencode(
                {"show": show, **({"q": q} if q else {}), "sort": sort, "page": page}
            )

        return page(
            request, "matches.html", "matches", rows=rows, show=show, q=q, counts=counts, days=MATCH_DAYS,
            sort=sort, sorts=MATCH_SORTS,
            page=number, pages=pages, total=total, size=PAGE_SIZE, link=link,
            link_page=lambda n: link(page=n),
        )  # fmt: skip

    def list_view(found: ApplicationPage, q: str, site: str, sort: str, asked: int) -> dict[str, Any]:
        """One Applications list page: its rows after search, site and sort, and its pages."""
        login = found.view is ApplicationView.LOGIN
        filters = ApplicationFilters(
            found.view, q.strip()[:80], site.strip()[:200], application_sort(sort), login
        )
        total = application_board.count_filtered(filters)
        number, pages = _page(asked, total)
        rows = application_board.rows(filters, PAGE_SIZE, (number - 1) * PAGE_SIZE)
        params = {"q": filters.query, "site": filters.site, "sort": filters.sort}
        kept = {key: value for key, value in params.items() if value}
        view: dict[str, Any] = {
            "app_page": found, "rows": rows, "filters": filters, "sorts": APPLICATION_SORTS,
            "sites": [(name, name) for name in application_board.sites(found.view)], "stages": BY_VIEW,
            "pending": actions.pending() if actions is not None else {},
            "page": number, "pages": pages, "total": total, "size": PAGE_SIZE,
            "link_page": lambda n: f"{found.href}?" + urlencode({**kept, "page": n}),
        }  # fmt: skip
        if login:
            offered = actions.login_sites() if actions is not None else []
            view["groups"] = login_groups(rows, application_board.count_by_site(filters), offered)
            view["controls"] = actions.controls() if actions is not None else None
        return view

    def moved(request: Request, path: str, drop: str = "") -> RedirectResponse:
        """An old address: the page it is now, with the same search and filters."""
        query = [(k, v) for k, v in request.query_params.multi_items() if k != drop]
        return RedirectResponse(path + (f"?{urlencode(query)}" if query else ""), status_code=302)

    @app.get("/applications", response_class=HTMLResponse, response_model=None)
    async def applications(request: Request, show: str = "") -> Response:
        if show:  # the address before each state had its own page
            return moved(request, (page_for(show) or PAGES[-1]).href, drop="show")
        waiting, moving = summary(application_board.counts())
        return page(request, "applications/summary.html", "applications", waiting=waiting, moving=moving)

    @app.get("/applications/{slug}", response_class=HTMLResponse, response_model=None)
    async def application_list(  # noqa: PLR0913 (individual query parameters)
        request: Request,
        slug: str,
        *,
        q: str = "",
        site: str = "",
        sort: str = "newest",
        page_no: int = Query(1, alias="page"),
    ) -> Response:
        found = page_for(slug)
        if found is None:
            raise HTTPException(404, "No such application view.")
        if found.slug != slug:
            return moved(request, found.href)
        return page(
            request, f"applications/{found.slug}.html", "applications", found.slug, crumb=found.title,
            **list_view(found, q, site, sort, page_no),
        )  # fmt: skip

    @app.get("/sources", response_model=None)
    async def sources_home(request: Request) -> Response:
        return moved(request, "/sources/readers")

    @app.get("/sources/{section}", response_class=HTMLResponse)
    async def sources(request: Request, section: str, page_no: int = Query(1, alias="page")) -> HTMLResponse:
        if section not in SOURCE_SECTIONS:
            raise HTTPException(404, "No such sources section.")
        total = queries.count_career_pages(list(site_map.career))
        number, pages = _page(page_no, total)
        view = sources_view(queries, clock(), PAGE_SIZE, (number - 1) * PAGE_SIZE, site_map)
        return page(
            request, f"sources/{section}.html", "sources", section, section=section,
            page=number, pages=pages, total=total, size=PAGE_SIZE,
            link_page=lambda n: f"/sources/companies?page={n}", **view,
        )  # fmt: skip

    def settings_page(  # noqa: PLR0913 (form-specific feedback)
        request: Request,
        values: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        mail_form: dict[str, str] | None = None,
        mail_result: tuple[bool, str] | None = None,
        *,
        section: str = "profile",
    ) -> HTMLResponse:
        profile = actions.profile() if actions is not None else None
        mail = actions.mail() if actions is not None else None
        return page(
            request,
            f"settings/{section}.html",
            "settings",
            section,
            status=400 if errors else 200,
            section=section,
            values=values if values is not None else form_values(profile),
            errors=errors or {},
            has_profile=profile is not None,
            controls=actions.controls() if actions is not None else None,
            login_sites=actions.login_sites() if actions is not None else [],
            remote_choices=REMOTE_CHOICES,
            mail=mail,
            mail_form=mail_form if mail_form is not None else mail_values(mail),
            mail_result=mail_result,
            mail_presets=PRESETS,
        )

    @app.get("/settings", response_model=None)
    async def settings_home(request: Request) -> Response:
        return moved(request, "/settings/profile")

    @app.get("/settings/{section}", response_class=HTMLResponse)
    async def settings(request: Request, section: str) -> HTMLResponse:
        if section not in SETTINGS_SECTIONS:
            raise HTTPException(404, "No such settings section.")
        return settings_page(request, section=section)

    # ---- changes ----

    @app.post("/settings/profile", response_model=None)
    async def save_profile(request: Request) -> Response:
        act, form = await checked_form(request)
        current = act.profile()
        resume_text = ""
        upload = form.get("resume")
        if isinstance(upload, UploadFile) and upload.filename:
            try:
                data = await upload.read(MAX_RESUME_BYTES + 1)
            finally:
                await upload.close()
            try:
                resume_text = act.resume_text(data)
            except ValueError as error:
                return settings_page(request, _text_fields(form), {"resume": str(error)})
        if current is None and not resume_text:
            return settings_page(
                request, _text_fields(form), {"resume": "Add your resume (PDF or .docx) to start."}
            )
        result = profile_from_form(_text_fields(form), current, resume_text)
        if result.profile is None:
            return settings_page(request, result.values, result.errors)
        act.save_profile(result.profile)
        return back("/settings/profile", "profile")

    @app.post("/settings/alerts", response_model=None)
    async def save_alerts(request: Request) -> Response:
        act, form = await checked_form(request)
        problem = act.set_auto_line(str(form.get("auto_line", "off")).strip() or "off")
        if problem:
            return settings_page(request, errors={"auto_line": problem}, section="automation")
        act.set_paused(form.get("paused") == "on")
        return back("/settings/automation", "alerts")

    @app.post("/settings/mail", response_model=None)
    async def save_mail(request: Request) -> Response:
        act, form = await checked_form(request)
        result = mail_from_form(_text_fields(form), act.mail())
        if result.settings is None:
            return settings_page(request, errors=result.errors, mail_form=result.values, section="email")
        act.save_mail(result.settings, result.password)
        return back("/settings/email", "mail")

    @app.post("/settings/mail/test", response_model=None)
    async def test_mail(request: Request) -> Response:
        act, form = await checked_form(request)
        result = mail_from_form(_text_fields(form), act.mail(), testing=True)
        if result.settings is None:
            return settings_page(request, errors=result.errors, mail_form=result.values, section="email")
        said = await act.test_mail(result.settings, result.password)
        return settings_page(
            request,
            mail_form=result.values,
            mail_result=(said.startswith("Connected"), said),
            section="email",
        )

    @app.post("/matches/queue", response_model=None)
    async def queue_matches(request: Request) -> Response:
        act, _ = await checked_form(request)
        batch = act.queue_matches()
        if batch.scheduled:
            return back("/applications/in-progress", "matching_queued")
        if batch.manual:
            return back("/applications/apply-yourself", "matching_manual")
        return back("/matches?show=alert", "matching_empty")

    @app.post("/jobs/{job_id}/{action}", response_model=None)
    async def choose(request: Request, job_id: str, action: str) -> Response:
        act, form = await checked_form(request)
        if action not in JOB_ACTIONS or not act.choose(job_id, action):
            raise HTTPException(404, "No such job or action.")
        return back(_own_page(str(form.get("next", ""))), action, f"job-{job_id}")

    @app.post("/applications/{job_id}/{decision}", response_model=None)
    async def decide(request: Request, job_id: str, decision: str) -> Response:
        act, form = await checked_form(request)
        if decision == "submit":
            return back(
                _own_page(str(form.get("next", "")), "/applications"),
                "submitted" if act.submit(job_id) else "gone",
            )
        if decision == "cancel":
            return back(
                _own_page(str(form.get("next", "")), "/applications"),
                "cancelled" if act.cancel(job_id) else "gone",
            )
        if decision == "finish":
            back_to = _own_page(str(form.get("next", "")), "/applications")
            return back(back_to, "finish" if act.finish(job_id) else "busy")
        if decision == "refill":
            back_to = _own_page(str(form.get("next", "")), "/applications")
            return back(back_to, "refill" if act.refill(job_id) else "gone")
        if decision == "applied":
            return back(
                _own_page(str(form.get("next", ""))), "applied" if act.mark_applied(job_id) else "gone"
            )
        raise HTTPException(404, "No such action.")

    # ---- without Telegram: questions, auto-apply's veto, site logins ----

    def activity_page(
        request: Request,
        section: str,
        errors: dict[str, dict[str, str]] | None = None,
        values: dict[str, str] | None = None,
        job_id: str | None = None,
    ) -> HTMLResponse:
        kind = request.query_params.get("kind", "")[:40]
        oldest = request.query_params.get("sort") == "oldest"
        return page(
            request, f"activity/{section}.html", "activity", section, status=400 if errors else 200,
            section=section,
            groups=inbox.questions(job_id) if section == "questions" else [],
            auto=inbox.auto_waiting() if section == "auto-apply" else [],
            notices=notices.recent(NOTICES_SHOWN, kind=kind, oldest=oldest) if section == "timeline" else [],
            kinds=[(k, notice_kind(k)) for k in notices.kinds()] if section == "timeline" else [], kind=kind,
            sort="oldest" if oldest else "newest", timeline_sorts=TIMELINE_SORTS.items(),
            errors=errors or {}, values=values or {},
        )  # fmt: skip

    @app.get("/activity", response_model=None)
    async def activity_home(request: Request, job: str | None = None) -> Response:
        return moved(request, "/activity/questions" if job else "/activity/timeline")

    @app.get("/activity/{section}", response_class=HTMLResponse)
    async def activity(request: Request, section: str, job: str | None = None) -> HTMLResponse:
        if section not in ACTIVITY_SECTIONS:
            raise HTTPException(404, "No such activity section.")
        return activity_page(request, section, job_id=job)

    @app.post("/questions/{job_id}", response_model=None)
    async def answer_questions(request: Request, job_id: str) -> Response:
        act, form = await checked_form(request)
        if form.get("decline") == "1":
            if not act.decline_questions(job_id):
                raise HTTPException(404, "No such job.")
            return back("/activity/questions", "declined")
        answers = {
            name.removeprefix(ANSWER): ", ".join(str(v).strip() for v in form.getlist(name) if str(v).strip())
            for name in set(form.keys())
            if name.startswith(ANSWER)
        }
        problems = act.answer_questions(job_id, answers)
        if problems:
            typed = {ANSWER + key: text for key, text in answers.items()}
            return activity_page(request, "questions", {job_id: problems}, typed, job_id=job_id)
        return back("/activity/questions", "answered", f"job-{job_id}")

    @app.post("/auto/{job_id}/{action}", response_model=None)
    async def auto_apply(request: Request, job_id: str, action: str) -> Response:
        act, _ = await checked_form(request)
        if action == "cancel":
            return back("/activity/auto-apply", "auto_cancelled" if act.cancel_auto(job_id) else "gone")
        if action == "now":
            return back("/activity/auto-apply", "auto_now" if act.apply_now(job_id) else "gone")
        raise HTTPException(404, "No such action.")

    @app.post("/settings/login/{site}", response_model=None)
    async def site_login(request: Request, site: str) -> Response:
        act, form = await checked_form(request)
        if site not in dict(act.login_sites()):
            raise HTTPException(404, "No such site.")
        back_to = _own_page(str(form.get("next", "")), "/settings/logins")
        return back(back_to, "login" if act.site_login(site) else "busy")

    @app.get("/applications/{job_id}/form.png", response_model=None)
    async def form_picture(job_id: str) -> Response:
        waiting = actions.pending().get(job_id) if actions is not None else None
        if waiting is None or not waiting.screenshot or data_dir is None:
            raise HTTPException(404)
        base = data_dir.resolve()
        path = (base / waiting.screenshot).resolve()
        if base not in path.parents or path.suffix.lower() != ".png" or not path.is_file():
            raise HTTPException(404)  # only screenshots Sarthi wrote into its own data folder
        return FileResponse(path, media_type="image/png")

    def document(job_id: str, letter: bool) -> FileResponse:
        """The exact PDF Sarthi made for this job: only a file inside its own data folder, else 404."""
        record = DocumentStore(conn).get(job_id)
        if record is None or data_dir is None:
            raise HTTPException(404)
        base = data_dir.resolve()
        path = (base / (record.letter_file if letter else record.resume_file)).resolve()
        if base not in path.parents or path.suffix.lower() != ".pdf" or not path.is_file():
            raise HTTPException(404)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.name)  # a plain name for the download
        return FileResponse(
            path, media_type="application/pdf", filename=name, content_disposition_type="inline"
        )

    @app.get("/applications/{job_id}/apply-kit", response_class=HTMLResponse, response_model=None)
    async def quick_apply(request: Request, job_id: str) -> Response:
        """Everything to apply by hand on a site Sarthi cannot fill: open, attach, copy, done."""
        kit = apply_kit(conn, data_dir, job_id, actions.profile() if actions is not None else None)
        if kit is None:
            raise HTTPException(404, "No such job.")
        return page(
            request, "applications/apply-kit.html", "applications", "apply-yourself", crumb="Quick apply",
            kit=kit,
        )  # fmt: skip

    @app.get("/applications/{job_id}/resume.pdf", response_model=None)
    async def resume_pdf(job_id: str) -> Response:
        return document(job_id, letter=False)

    @app.get("/applications/{job_id}/cover-letter.pdf", response_model=None)
    async def cover_letter_pdf(job_id: str) -> Response:
        return document(job_id, letter=True)

    return app
