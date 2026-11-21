# Security layer adapted from JobRadar (MIT), src/jobradar/web/app.py @ fdd739b (trusted hosts, headers,
# versioned assets) and the time helper from src/jobradar/web/views.py (``ago``). Copyright (c) ThePlator.
# Modifications (c) Sarthi contributors: Sarthi's pages and data, read-only, no JavaScript.
"""The dashboard's web app: overview, matches, applications and sources, read from Sarthi's database.

It runs on this computer only and only reads. A browser sends requests to 127.0.0.1 from any site you have
open, so requests naming any other host are refused (that stops a site posing as localhost, "DNS rebinding"),
pages may not be framed or load anything from elsewhere, and there is nothing to post: no forms, no actions.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from sarthi import __version__
from sarthi.clock import Clock, utcnow
from sarthi.db.dashboard import DashboardQueries

HERE = Path(__file__).parent
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; "
        "frame-ancestors 'none'; form-action 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
MATCH_DAYS = 7
DECISIONS = {"alert": ("alert",), "digest": ("digest",), "all": ("alert", "digest")}
MINUTES_PER_HOUR, HOURS_SHOWN = 60, 48


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
    """A connection that cannot change anything (the dashboard only reads)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def create_app(conn: sqlite3.Connection, clock: Clock = utcnow) -> FastAPI:
    """The dashboard over ``conn`` (open it with ``read_only``)."""
    queries = DashboardQueries(conn)
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

    def page(request: Request, name: str, nav: str, **context: Any) -> HTMLResponse:
        now = clock()
        start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        overview = queries.overview(start)
        return templates.TemplateResponse(
            request,
            name,
            {"nav": nav, "ov": overview, "ago": lambda iso: ago(iso, now), **context},
        )

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> HTMLResponse:
        return page(request, "overview.html", "overview")

    @app.get("/matches", response_class=HTMLResponse)
    async def matches(request: Request, show: str = "all") -> HTMLResponse:
        show = show if show in DECISIONS else "all"
        rows = queries.matches(DECISIONS[show], clock() - timedelta(days=MATCH_DAYS))
        return page(request, "matches.html", "matches", rows=rows, show=show, days=MATCH_DAYS)

    @app.get("/applications", response_class=HTMLResponse)
    async def applications(request: Request) -> HTMLResponse:
        return page(request, "applications.html", "applications", rows=queries.applications())

    @app.get("/sources", response_class=HTMLResponse)
    async def sources(request: Request) -> HTMLResponse:
        return page(request, "sources.html", "sources", rows=queries.sources())

    return app
