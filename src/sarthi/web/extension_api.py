"""The browser extension's API: fill a form the user applies on themselves, in their own browser.

Every website the user opens can send requests to 127.0.0.1, so each call here must:
- come from a browser extension (its Origin is chrome-extension:// or moz-extension://, which no website can
  send), on a host of this computer (the dashboard's TrustedHostMiddleware);
- carry the key the extension got by pairing with a one-time code shown on the dashboard (pairing tries are
  limited, and a new pairing ends the old key).
The extension gets answers and this job's own PDFs, never a path on this computer, and it never submits.
"""

from __future__ import annotations

import time
from typing import Any, get_args

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from sarthi.jobs.forms import FieldType, FormField
from sarthi.profile.assist import FILE_KINDS, MAX_FIELDS
from sarthi.web.actions import DashboardActions

EXTENSION_ORIGINS = ("chrome-extension://", "moz-extension://")
PAIR_TRIES = 10  # wrong codes allowed per window, from any extension
PAIR_WINDOW_S = 600.0
MAX_LABEL = 500
MAX_OPTIONS = 100
MAX_VALUE = 5000
_TYPES = frozenset(get_args(FieldType))


def _from_extension(request: Request) -> bool:
    return request.headers.get("origin", "").startswith(EXTENSION_ORIGINS)


def _short(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def form_fields(raw: Any) -> list[FormField]:
    """The scanned fields the extension sent, checked and cut to size (anything odd is dropped)."""
    if not isinstance(raw, list):
        raise HTTPException(422, "fields must be a list")
    fields: list[FormField] = []
    for item in raw[:MAX_FIELDS]:
        if not isinstance(item, dict) or not item.get("key"):
            continue
        kind = str(item.get("type") or "text")
        given = item.get("options")
        options: list[Any] = given if isinstance(given, list) else []
        fields.append(
            FormField(
                key=_short(item["key"], 80),
                label=_short(item.get("label"), MAX_LABEL),
                type=kind if kind in _TYPES else "unknown",  # type: ignore[arg-type]
                required=bool(item.get("required")),
                options=tuple(_short(o, MAX_LABEL) for o in options[:MAX_OPTIONS]),
            )
        )
    return fields


def add_extension_routes(app: FastAPI, actions: DashboardActions | None) -> None:  # noqa: PLR0915  (one route each)
    """The /ext/ routes on the dashboard ``app`` (none work without the running app's ``actions``)."""
    failures: list[float] = []

    def running() -> DashboardActions:
        if actions is None:
            raise HTTPException(503, "Sarthi is not running (start it with: sarthi run)")
        return actions

    def allowed(request: Request) -> DashboardActions:
        """The checks every call after pairing must pass."""
        live = running()
        if not _from_extension(request):
            raise HTTPException(403, "Only the Sarthi browser extension may call this.")
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not key or not live.extension_allowed(key.strip()):
            raise HTTPException(401, "Pair the extension again: Settings > Browser extension.")
        return live

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(422, "expected JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(422, "expected a JSON object")
        return data

    @app.post("/ext/pair")
    async def pair(request: Request) -> JSONResponse:
        live = running()
        if not _from_extension(request):
            raise HTTPException(403, "Only the Sarthi browser extension may pair.")
        now = time.monotonic()
        failures[:] = [t for t in failures if now - t < PAIR_WINDOW_S]
        if len(failures) >= PAIR_TRIES:
            raise HTTPException(429, "Too many wrong codes: wait 10 minutes and use a new code.")
        code = _short((await body(request)).get("code"), 32).strip().upper()
        key = live.pair_extension(code) if code else None
        if key is None:
            failures.append(now)
            raise HTTPException(
                403, "That code is wrong or expired: use the code on Settings > Browser extension."
            )
        return JSONResponse({"key": key})

    @app.get("/ext/jobs")
    async def jobs(request: Request, url: str = "") -> JSONResponse:
        found = allowed(request).extension_jobs(_short(url, 2000))
        return JSONResponse(
            {
                "matched": found.matched,
                "jobs": [
                    {"id": j.job_id, "title": j.title, "company": j.company, "note": j.note}
                    for j in found.jobs
                ],
            }
        )

    @app.get("/ext/scan-config")
    async def scan_config(request: Request, url: str = "") -> JSONResponse:
        return JSONResponse(allowed(request).extension_scan_config(_short(url, 2000)))

    @app.post("/ext/fill")
    async def fill(request: Request) -> JSONResponse:
        live = allowed(request)
        data = await body(request)
        job_id = _short(data.get("job_id"), 80)
        result = live.extension_fill(job_id, form_fields(data.get("fields")))
        if result is None:
            raise HTTPException(404, "Sarthi does not know that job any more.")
        return JSONResponse(
            {
                "answers": {
                    k: {"value": a.value[:MAX_VALUE], "source": a.source} for k, a in result.answers.items()
                },
                "missing": result.missing,
                "resume": result.resume,
                "cover": result.cover,
            }
        )

    @app.get("/ext/file/{job_id}/{kind}", response_model=None)
    async def document(request: Request, job_id: str, kind: str) -> Response:
        live = allowed(request)
        if kind not in FILE_KINDS:
            raise HTTPException(404, "unknown document")
        path = live.extension_file(job_id, kind)
        if path is None:
            raise HTTPException(404, "This job's document is not made yet.")
        return FileResponse(path, media_type="application/pdf", filename=path.name)

    @app.post("/ext/applied")
    async def applied(request: Request) -> JSONResponse:
        live = allowed(request)
        data = await body(request)
        sent = data.get("answers")
        raw: list[Any] = sent if isinstance(sent, list) else []
        given = [
            (_short(a.get("label"), MAX_LABEL), _short(a.get("value"), MAX_VALUE))
            for a in raw[:MAX_FIELDS]
            if isinstance(a, dict)
        ]
        if not live.extension_applied(_short(data.get("job_id"), 80), given):
            raise HTTPException(404, "Sarthi does not know that job any more.")
        return JSONResponse({"ok": True})

    @app.get("/extension.zip", response_model=None)
    async def package() -> Response:
        """The extension itself (its code, no key or data): downloaded from Settings > Browser extension."""
        return Response(
            running().extension_package(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="sarthi-extension.zip"'},
        )
