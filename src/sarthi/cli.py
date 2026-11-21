"""Command-line entry point: ``sarthi <command>``."""

from __future__ import annotations

import asyncio
import contextlib

import typer

from sarthi import __version__
from sarthi.app import BROWSER_PROFILE, SetupError, build
from sarthi.apply.engine import LOGIN_PAGES
from sarthi.apply.engine import login as login_to
from sarthi.chat.pairing import CODE_LIFETIME, new_code
from sarthi.clock import utcnow
from sarthi.config import get_settings
from sarthi.db.chat import ChatStore
from sarthi.db.connection import connect
from sarthi.log import setup_logging

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
    """Show a one-time code that links your Telegram chat to Sarthi."""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        code = new_code(ChatStore(conn), utcnow())
    finally:
        conn.close()
    minutes = int(CODE_LIFETIME.total_seconds() // 60)
    typer.echo(f"In Telegram, open your Sarthi bot and send:\n\n    /start {code}\n")
    typer.echo(f"The code works once and expires in {minutes} minutes. Then run: uv run sarthi run")


@app.command()
def run() -> None:
    """Run Sarthi: job sources, matching, Telegram alerts and the daily digest (Ctrl+C to stop)."""
    try:
        sarthi = build(get_settings())
    except SetupError as error:
        typer.echo(f"Cannot start: {error}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("Sarthi is running. Press Ctrl+C to stop.")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(sarthi.run())


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
    typer.echo(f"A browser window is opening. Log in to {site} there; it closes by itself once you're in.")
    done = asyncio.run(login_to(site, settings.data_dir / BROWSER_PROFILE))
    typer.echo(
        "Logged in: Sarthi can now apply there." if done else "Not logged in (window closed or timed out)."
    )
