"""Command-line entry point: ``sarthi <command>``."""

from __future__ import annotations

import asyncio
import contextlib

import typer

from sarthi import __version__
from sarthi.app import SetupError, build
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
    typer.echo(f"The code works once and expires in {minutes} minutes. Then run: sarthi bot")


@app.command()
def bot() -> None:
    """Run the Telegram bot with matching, alerts and the daily digest (Ctrl+C to stop)."""
    try:
        sarthi = build(get_settings())
    except SetupError as error:
        typer.echo(f"Cannot start: {error}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("Sarthi is running. Press Ctrl+C to stop.")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(sarthi.run())
