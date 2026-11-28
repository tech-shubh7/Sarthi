"""Serves the dashboard with Uvicorn on 127.0.0.1, on the configured port or the next free one.

It runs on its own (``sarthi dashboard``), or as one more task inside ``sarthi run``.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import socket
from collections.abc import Generator
from pathlib import Path
from typing import Any

from sarthi.web.actions import DashboardActions
from sarthi.web.app import create_app, read_only
from sarthi.web.sources_view import SiteMap

log = logging.getLogger(__name__)

HOST = "127.0.0.1"  # this computer only; the dashboard has no login
ALL_INTERFACES = "0.0.0.0"  # noqa: S104  (inside a container only: compose publishes it to the host's 127.0.0.1)
PORT_TRIES = 10  # 8765 busy: try 8766 ... 8774


class NoFreePortError(Exception):
    """Every port tried is in use."""


def _bindable(port: int, host: str = HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # As Uvicorn binds: a port still cooling down from the last run (TIME_WAIT) is free to use, so a quick
        # restart keeps the dashboard at its usual address instead of moving to the next port.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            if e.errno in (errno.EADDRINUSE, errno.EACCES):
                return False
            raise
    return True


def free_port(port: int, host: str = HOST) -> int:
    """``port`` if nothing uses it, else the next free one within ``PORT_TRIES``."""
    for candidate in range(port, min(port + PORT_TRIES, 65536)):
        if _bindable(candidate, host):
            return candidate
    raise NoFreePortError(
        f"ports {port}-{port + PORT_TRIES - 1} are all in use; set SARTHI_DASHBOARD_PORT to a free one"
    )


def make_server(  # noqa: PLR0913  (where, how, and what it may change)
    db_path: Path,
    port: int,
    *,
    host: str = HOST,
    own_signals: bool = True,
    actions: DashboardActions | None = None,
    data_dir: Path | None = None,
    sites: SiteMap | None = None,
) -> Any:
    """A Uvicorn server for the dashboard on ``host`` (127.0.0.1, or all interfaces inside a container).

    ``own_signals=False`` (inside ``sarthi run``) leaves Ctrl+C to Sarthi, which stops it via ``should_exit``.
    """
    if host not in (HOST, ALL_INTERFACES):
        raise ValueError(f"dashboard host must be {HOST} or {ALL_INTERFACES}, not {host!r}")
    import uvicorn  # noqa: PLC0415  (only when the dashboard runs)

    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Generator[None]:
            if own_signals:
                with super().capture_signals():
                    yield
            else:
                yield

    chosen = free_port(port, host)
    if chosen != port:
        log.warning("Port %d is in use; the dashboard took %d instead", port, chosen)
    # Under Docker the port inside the container is not the one the user opens (docker-compose.yml passes it).
    shown = os.environ.get("SARTHI_HOST_DASHBOARD_PORT", "").strip()
    shown_port = int(shown) if shown.isdigit() else chosen
    log.info("Dashboard: http://%s:%d (open it in your browser; this computer only)", HOST, shown_port)
    config = uvicorn.Config(
        create_app(read_only(db_path), actions=actions, data_dir=data_dir, sites=sites),
        host=host,
        port=chosen,
        log_level="warning",
        access_log=False,
        lifespan="off",
        server_header=False,
    )
    return Server(config)
