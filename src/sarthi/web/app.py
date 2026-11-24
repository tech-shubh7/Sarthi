# Security layer adapted from JobRadar (MIT), src/jobradar/web/app.py @ fdd739b (trusted hosts, headers,
# versioned assets) and the time helper from src/jobradar/web/views.py (``ago``). Copyright (c) ThePlator.
# Modifications (c) Sarthi contributors: Sarthi's pages and data, settings and actions, no JavaScript.
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
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware

from sarthi import __version__
from sarthi.clock import Clock, utcnow
from sarthi.db.dashboard import DashboardQueries
from sarthi.web.actions import DashboardActions
from sarthi.web.settings_form import REMOTE_CHOICES, form_values, profile_from_form

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
BACK_TO = ("/matches", "/", "/applications")
# What a change did, by a fixed key in the address: nothing a page elsewhere puts in a link is ever shown.
DONE = {
    "profile": "Saved. Recent jobs are being matched again with your new profile.",
    "alerts": "Saved.",
    "apply": "Apply started: the tailored resume comes first, then the filled form waits for your Submit.",
    "skip": "Skipped.",
    "later": "Saved for later.",
    "submitted": "Submitting. The result shows here and in Telegram.",
    "cancelled": "Cancelled: nothing was sent.",
    "gone": "That is no longer waiting.",
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
    host = (urlparse(source).hostname or "").lower()
    return host in LOCAL_NAMES


def _page(asked: int, total: int) -> tuple[int, int]:
    """(this page, number of pages): an out-of-range page number shows the nearest real page."""
    pages = max(1, -(-total // PAGE_SIZE))
    return min(max(asked, 1), pages), pages


def _text_fields(form: FormData) -> dict[str, str]:
    return {key: value for key, value in form.items() if isinstance(value, str)}


def create_app(  # noqa: PLR0915  (one route per page and action)
    conn: sqlite3.Connection,
    clock: Clock = utcnow,
    *,
    actions: DashboardActions | None = None,
    data_dir: Path | None = None,
) -> FastAPI:
    """The dashboard over ``conn`` (open it with ``read_only``); ``actions`` None: nothing can be changed."""
    queries = DashboardQueries(conn)
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
    templates.env.globals.update(asset=asset, version=__version__)

    def page(request: Request, name: str, nav: str, status: int = 200, **context: Any) -> HTMLResponse:
        now = clock()
        start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        overview = queries.overview(start)
        done = DONE.get(request.query_params.get("done", ""), "")
        return templates.TemplateResponse(
            request,
            name,
            {
                "nav": nav,
                "ov": overview,
                "ago": lambda iso: ago(iso, now),
                "csrf": token,
                "can_edit": actions is not None,
                "done": done,
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

    def back(path: str, done: str) -> RedirectResponse:
        return RedirectResponse(f"{path}?done={done}", status_code=303)

    # ---- pages ----

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> HTMLResponse:
        return page(request, "overview.html", "overview")

    @app.get("/matches", response_class=HTMLResponse)
    async def matches(
        request: Request, show: str = "all", q: str = "", page_no: int = Query(1, alias="page")
    ) -> HTMLResponse:
        show = show if show in DECISIONS else "all"
        q = q.strip()[:80]
        since = clock() - timedelta(days=MATCH_DAYS)
        counts = {key: queries.count_matches(kinds, since, q) for key, kinds in DECISIONS.items()}
        total = counts[show]
        number, pages = _page(page_no, total)
        rows = queries.matches(DECISIONS[show], since, PAGE_SIZE, offset=(number - 1) * PAGE_SIZE, query=q)

        def link(show: str = show, page: int = number) -> str:
            return "/matches?" + urlencode({"show": show, **({"q": q} if q else {}), "page": page})

        return page(
            request, "matches.html", "matches", rows=rows, show=show, q=q, counts=counts, days=MATCH_DAYS,
            page=number, pages=pages, total=total, size=PAGE_SIZE, link=link,
            link_page=lambda n: link(page=n),
        )  # fmt: skip

    @app.get("/applications", response_class=HTMLResponse)
    async def applications(request: Request, page_no: int = Query(1, alias="page")) -> HTMLResponse:
        pending = actions.pending() if actions is not None else {}
        total = queries.count_applications()
        number, pages = _page(page_no, total)
        rows = queries.applications(PAGE_SIZE, offset=(number - 1) * PAGE_SIZE)
        return page(
            request, "applications.html", "applications", rows=rows, pending=pending,
            waiting=queries.applications_for(list(pending)), page=number, pages=pages, total=total,
            size=PAGE_SIZE, link_page=lambda n: f"/applications?page={n}",
        )  # fmt: skip

    @app.get("/sources", response_class=HTMLResponse)
    async def sources(request: Request) -> HTMLResponse:
        return page(request, "sources.html", "sources", rows=queries.sources())

    def settings_page(
        request: Request, values: dict[str, str] | None = None, errors: dict[str, str] | None = None
    ) -> HTMLResponse:
        profile = actions.profile() if actions is not None else None
        return page(
            request,
            "settings.html",
            "settings",
            status=400 if errors else 200,
            values=values if values is not None else form_values(profile),
            errors=errors or {},
            has_profile=profile is not None,
            controls=actions.controls() if actions is not None else None,
            remote_choices=REMOTE_CHOICES,
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request) -> HTMLResponse:
        return settings_page(request)

    # ---- changes ----

    @app.post("/settings/profile", response_model=None)
    async def save_profile(request: Request) -> Response:
        act, form = await checked_form(request)
        current = act.profile()
        resume_text = ""
        upload = form.get("resume")
        if isinstance(upload, UploadFile) and upload.filename:
            data = await upload.read(MAX_RESUME_BYTES + 1)
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
        return back("/settings", "profile")

    @app.post("/settings/alerts", response_model=None)
    async def save_alerts(request: Request) -> Response:
        act, form = await checked_form(request)
        act.set_paused(form.get("paused") == "on")
        problem = act.set_auto_line(str(form.get("auto_line", "off")).strip() or "off")
        if problem:
            return settings_page(request, errors={"auto_line": problem})
        return back("/settings", "alerts")

    @app.post("/jobs/{job_id}/{action}", response_model=None)
    async def choose(request: Request, job_id: str, action: str) -> Response:
        act, form = await checked_form(request)
        if action not in JOB_ACTIONS or not act.choose(job_id, action):
            raise HTTPException(404, "No such job or action.")
        target = str(form.get("next", "/matches"))
        return back(target if target in BACK_TO else "/matches", action)

    @app.post("/applications/{job_id}/{decision}", response_model=None)
    async def decide(request: Request, job_id: str, decision: str) -> Response:
        act, _ = await checked_form(request)
        if decision == "submit":
            return back("/applications", "submitted" if act.submit(job_id) else "gone")
        if decision == "cancel":
            return back("/applications", "cancelled" if act.cancel(job_id) else "gone")
        raise HTTPException(404, "No such action.")

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

    return app
