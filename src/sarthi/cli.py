"""Command-line entry point: ``sarthi <command>``."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Coroutine
from typing import Any

import typer

from sarthi import __version__
from sarthi.app import BROWSER_PROFILE, SetupError, build, site_map
from sarthi.apply.engine import LOGIN_PAGES
from sarthi.apply.engine import login as login_to
from sarthi.apply.screen import ScreenError, login_screen
from sarthi.chat.pairing import CODE_LIFETIME, new_code
from sarthi.clock import utcnow
from sarthi.config import get_settings
from sarthi.db.chat import ChatStore
from sarthi.db.connection import connect
from sarthi.log import setup_logging
from sarthi.web.server import NoFreePortError, make_server

app = typer.Typer(help="Sarthi: finds, matches and applies to jobs for you.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Configure logging before any command runs."""
    setup_logging(get_settings().log_level)


@app.command()
def version() -> None:
    """Print the installed Sarthi version."""
    typer.echo(__version__)


@app.command()
def pair() -> None:
    """Show a one-time code that links your Telegram chat to Sarthi (your own bot only)."""
    settings = get_settings()
    if settings.telegram_bot_token is None and settings.relay_url:
        typer.echo(
            "With the shared Sarthi bot there is nothing to pair by hand: start Sarthi and open the link it\n"
            f"shows in its log and at http://127.0.0.1:{settings.dashboard_port}"
        )
        return
    conn = connect(settings.db_path)
    try:
        code = new_code(ChatStore(conn), utcnow())
    finally:
        conn.close()
    minutes = int(CODE_LIFETIME.total_seconds() // 60)
    typer.echo(f"In Telegram, open your Sarthi bot and send:\n\n    /start {code}\n")
    typer.echo(f"The code works once and expires in {minutes} minutes. Then run: uv run sarthi run")


@app.command()
def relay() -> None:
    """Run the shared-bot relay (the project's server, not for users): one bot for every Sarthi."""
    import uvicorn  # noqa: PLC0415  (only for the relay)

    from sarthi.chat.telegram.bot import COMMANDS  # noqa: PLC0415
    from sarthi.relay.server import Relay, RelayStore, create_relay_app  # noqa: PLC0415

    settings = get_settings()
    if settings.relay_bot_token is None:
        typer.echo("Cannot start: set SARTHI_RELAY_BOT_TOKEN (the shared bot's token)", err=True)
        raise typer.Exit(code=1)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    shared = Relay(RelayStore(settings.data_dir / "relay.db"), settings.relay_bot_token, commands=COMMANDS)
    web = create_relay_app(shared, trust_forwarded=settings.relay_trust_forwarded)

    async def serve() -> None:
        await shared.start()
        typer.echo(f"Relay for @{shared.bot_username} on port {settings.relay_port}. Ctrl+C to stop.")
        config = uvicorn.Config(
            web,
            host="0.0.0.0",  # noqa: S104  (a public server, behind HTTPS)
            port=settings.relay_port,
            log_level="warning",
            access_log=False,
            server_header=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        poller = asyncio.create_task(shared.poll())
        try:
            await server.serve()
        finally:
            shared.stop()
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller
            await shared.aclose()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())


@app.command()
def run() -> None:
    """Run Sarthi: job sources, matching, Telegram alerts and the daily digest (Ctrl+C to stop)."""
    try:
        sarthi = build(get_settings())
    except SetupError as error:
        typer.echo(f"Cannot start: {error}", err=True)
        raise typer.Exit(code=1) from None
    # The dashboard's address is logged by the web server once it has its port: 8765 may be taken, and
    # printing the setting here sent people to another program (2026-11-28).
    typer.echo("Sarthi is running. Press Ctrl+C to stop.")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(until_terminated(sarthi.run()))


async def until_terminated(work: Coroutine[Any, Any, None]) -> None:
    """Run ``work``; SIGTERM (``docker stop``, systemd) cancels it as Ctrl+C does.

    Without this the process dies at once on SIGTERM: tasks in hand stay "running" in the database and
    an open browser is killed mid-form. Cancelling lets every worker hand its task back first.
    """
    main = asyncio.ensure_future(work)
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError, RuntimeError):  # (Windows: no loop signal handlers)
        loop.add_signal_handler(signal.SIGTERM, main.cancel)
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await main
    finally:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGTERM)


@app.command()
def dashboard() -> None:
    """Open the read-only dashboard on its own, without running Sarthi (Ctrl+C to stop)."""
    settings = get_settings()
    if not settings.db_path.exists():
        typer.echo("No data yet: run `uv run sarthi run` first.", err=True)
        raise typer.Exit(code=1)
    try:
        server = make_server(
            settings.db_path, settings.dashboard_port, host=settings.dashboard_host, sites=site_map()
        )
    except NoFreePortError as error:
        typer.echo(f"Cannot start the dashboard: {error}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Dashboard: http://127.0.0.1:{server.config.port} (this computer only). Ctrl+C to stop.")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(server.serve())


@app.command()
def catchup(
    days: int = typer.Option(1, min=1, max=7, help="How many days back to search (1-7)."),
) -> None:
    """Search every source for the last N days, send the results to Telegram in one batch, then exit.

    For a computer that is on only part of the day: run it from cron or a startup task.
    """
    try:
        sarthi = build(get_settings())
    except SetupError as error:
        typer.echo(f"Cannot start: {error}", err=True)
        raise typer.Exit(code=1) from None
    problem = sarthi.catchup_problem()
    if problem:
        sarthi.conn.close()
        typer.echo(f"Cannot catch up: {problem}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Searching every source for the last {days} day(s)… results will arrive in Telegram.")
    with contextlib.suppress(KeyboardInterrupt):
        sent = asyncio.run(sarthi.run_catchup(days * 24))
        typer.echo(
            "Done: results sent to Telegram." if sent else "Stopped: results took too long (try again)."
        )


@app.command()
def login(site: str = typer.Argument(..., help="linkedin or naukri")) -> None:
    """Log in once to a job site in a browser window, so Sarthi can apply there with your account.

    You type your password into the site itself; Sarthi never sees it. Stop `sarthi run` first (the saved
    browser profile can be used by one program at a time).
    """
    site = site.strip().lower()
    if site not in LOGIN_PAGES:
        typer.echo(f"Unknown site {site!r}. Choose one of: {', '.join(LOGIN_PAGES)}", err=True)
        raise typer.Exit(code=2)
    settings = get_settings()

    async def log_in() -> bool:
        async with login_screen(settings.screen_port, settings.dashboard_host) as screen:
            if screen.url is None:
                typer.echo(f"A browser window is opening. Log in to {site} there; it closes once you're in.")
            else:
                typer.echo(f"Open this in your browser and log in to {site} there:\n\n    {screen.url}\n")
            return await login_to(
                site, settings.data_dir / BROWSER_PROFILE, env=screen.env, headless=settings.apply_headless
            )

    try:
        done = asyncio.run(log_in())
    except ScreenError as error:
        typer.echo(f"Cannot show a browser: {error}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        "Logged in: Sarthi can now apply there." if done else "Not logged in (window closed or timed out)."
    )
