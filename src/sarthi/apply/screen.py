"""A screen for logging in to a job site where there is none (inside Docker), shown in the user's own browser.

Logging in to LinkedIn or Naukri needs a visible browser: the user types their password into the site itself.
On a desktop the window simply opens. Inside a container there is no desktop, so for the few minutes of a
login Sarthi starts a virtual screen (Xvfb), shares it over VNC on the container's loopback (x11vnc) and
serves it as a web page (noVNC via websockify) on 127.0.0.1:6080, which the user opens. A random password,
carried in the link, guards the screen, and everything stops when the login ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import secrets
import shutil
import signal
import socket
import sys
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

TOOLS = ("Xvfb", "x11vnc", "websockify")
NOVNC_DIRS = (Path("/usr/share/novnc"), Path("/usr/share/webapps/novnc"))
SIZE = "1280x900x24"
FIRST_DISPLAY, LAST_DISPLAY = 99, 130
VNC_PORT_BASE = 5900
START_WAIT_S = 10.0
STOP_WAIT_S = 5.0
VNC_PASSWORD_CHARS = 8  # the VNC protocol uses at most 8
PR_SET_PDEATHSIG = 1  # prctl option: a signal for the child when its parent dies


class ScreenError(Exception):
    """No screen can be shown here."""


@dataclass(frozen=True, slots=True)
class Screen:
    """Where the login browser shows: a link to open (virtual screen), or None (a real window opens)."""

    url: str | None
    env: dict[str, str] | None  # environment for the browser (DISPLAY of the virtual screen)


def has_display() -> bool:
    """True when a real window can open (a desktop, WSLg, an X or Wayland session).

    Windows and macOS always have one (they never set DISPLAY): looking for an X server there sent a login
    to a virtual screen that cannot exist.
    """
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _free_display() -> int:
    for number in range(FIRST_DISPLAY, LAST_DISPLAY):
        if not Path(f"/tmp/.X11-unix/X{number}").exists() and not Path(f"/tmp/.X{number}-lock").exists():  # noqa: S108
            return number
    raise ScreenError("no free display number for a virtual screen")


def _novnc_dir() -> Path:
    for path in NOVNC_DIRS:
        if (path / "vnc.html").exists():
            return path
    raise ScreenError("noVNC is not installed (apt install novnc)")


async def _until(check: object, what: str) -> None:
    end = asyncio.get_running_loop().time() + START_WAIT_S
    while asyncio.get_running_loop().time() < end:
        if check():  # type: ignore[operator]
            return
        await asyncio.sleep(0.1)
    raise ScreenError(f"{what} did not start")


def _die_with_parent() -> None:
    """In the child: be stopped when Sarthi dies, even killed outright (Linux; elsewhere a no-op)."""
    with contextlib.suppress(OSError, AttributeError):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


def _read_line(fd: int) -> str:
    """What a process writes to the pipe ``fd``, up to its first newline (or until it closes the pipe)."""
    chunks = b""
    while b"\n" not in chunks:
        chunk = os.read(fd, 64)
        if not chunk:
            break
        chunks += chunk
    return chunks.decode("ascii", "replace")


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


async def _stop(processes: list[asyncio.subprocess.Process]) -> None:
    for process in reversed(processes):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_WAIT_S)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


@contextlib.asynccontextmanager
async def login_screen(port: int = 6080, host: str = "127.0.0.1") -> AsyncIterator[Screen]:
    """A screen for one login: the real one when there is a desktop, else a virtual one at ``port``.

    ``host`` is where the web page listens: 127.0.0.1, or 0.0.0.0 inside a container (which publishes it to
    the host's 127.0.0.1 only).
    """
    if has_display():
        yield Screen(None, None)
        return
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if missing:
        raise ScreenError(
            f"there is no screen here and {', '.join(missing)} is not installed to make one "
            "(the Docker image has it; or run `sarthi login` on a computer with a desktop)"
        )
    web = _novnc_dir()
    if _listening(port):
        raise ScreenError(f"port {port} is in use (another login open?)")
    display = _free_display()
    vnc_port = VNC_PORT_BASE + display
    password = secrets.token_urlsafe(16)[:VNC_PASSWORD_CHARS]
    processes: list[asyncio.subprocess.Process] = []
    try:
        # Xvfb writes its display number to -displayfd once it accepts clients: a readiness signal that works
        # even where /tmp/.X11-unix is read-only (WSLg mounts it so; Linux then uses its abstract socket).
        ready_read, ready_write = os.pipe()
        try:
            processes.append(
                await asyncio.create_subprocess_exec(
                    "Xvfb",
                    f":{display}",
                    "-displayfd",
                    str(ready_write),
                    "-screen",
                    "0",
                    SIZE,
                    "-nolisten",
                    "tcp",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    pass_fds=(ready_write,),
                    preexec_fn=_die_with_parent,
                )
            )
        finally:
            os.close(ready_write)
        try:
            said = await asyncio.wait_for(asyncio.to_thread(_read_line, ready_read), START_WAIT_S)
        except TimeoutError:
            said = ""
        finally:
            os.close(ready_read)
        if said.strip() != str(display):
            raise ScreenError("the virtual screen did not start")
        fd, secret_file = tempfile.mkstemp(prefix="sarthi-vnc-")
        with os.fdopen(fd, "w") as file:
            file.write(password + "\n")
        processes.append(
            await asyncio.create_subprocess_exec(
                "x11vnc",
                "-display",
                f":{display}",
                "-rfbport",
                str(vnc_port),
                "-localhost",
                "-passwdfile",
                f"rm:{secret_file}",
                "-forever",
                "-shared",
                "-noxdamage",
                "-quiet",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=_die_with_parent,
            )
        )
        await _until(lambda: _listening(vnc_port), "the screen sharing")
        processes.append(
            await asyncio.create_subprocess_exec(
                "websockify",
                "--web",
                str(web),
                f"{host}:{port}",
                f"127.0.0.1:{vnc_port}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=_die_with_parent,
            )
        )
        await _until(lambda: _listening(port), "the screen's web page")
        url = f"http://127.0.0.1:{port}/vnc.html?autoconnect=true&resize=scale&password={password}"
        yield Screen(url, {**os.environ, "DISPLAY": f":{display}"})
    finally:
        await _stop(processes)
