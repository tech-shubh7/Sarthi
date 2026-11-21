# Adapted from JobRadar (MIT), src/jobradar/web/server.py @ fdd739b.
# Copyright (c) ThePlator. Modifications (c) Sarthi contributors: always this computer only (127.0.0.1).
"""Serves the dashboard with Uvicorn on 127.0.0.1, on the configured port or the next free one.

It runs on its own (``sarthi dashboard``), or as one more task inside ``sarthi run``.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import socket
from collections.abc import Generator
from pathlib import Path
from typing import Any

from sarthi.web.app import create_app, read_only

log = logging.getLogger(__name__)

HOST = "127.0.0.1"  # this computer only; the dashboard has no login
PORT_TRIES = 10  # 8765 busy: try 8766 ... 8774


class NoFreePortError(Exception):
    """Every port tried is in use."""


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
        except OSError as e:
            if e.errno in (errno.EADDRINUSE, errno.EACCES):
                return False
            raise
    return True


def free_port(port: int) -> int:
    """``port`` if nothing uses it, else the next free one within ``PORT_TRIES``."""
    for candidate in range(port, min(port + PORT_TRIES, 65536)):
        if _bindable(candidate):
            return candidate
    raise NoFreePortError(
        f"ports {port}-{port + PORT_TRIES - 1} are all in use; set SARTHI_DASHBOARD_PORT to a free one"
    )


def make_server(db_path: Path, port: int, *, own_signals: bool = True) -> Any:
    """A Uvicorn server for the dashboard.

    ``own_signals=False`` (inside ``sarthi run``) leaves Ctrl+C to Sarthi, which stops it via ``should_exit``.
    """
    import uvicorn  # noqa: PLC0415  (only when the dashboard runs)

    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Generator[None]:
            if own_signals:
                with super().capture_signals():
                    yield
            else:
                yield

    chosen = free_port(port)
    if chosen != port:
        log.warning("Port %d is in use; the dashboard took %d instead", port, chosen)
    log.info("Dashboard: http://%s:%d", HOST, chosen)
    config = uvicorn.Config(
        create_app(read_only(db_path)),
        host=HOST,
        port=chosen,
        log_level="warning",
        access_log=False,
        lifespan="off",
        server_header=False,
    )
    return Server(config)
