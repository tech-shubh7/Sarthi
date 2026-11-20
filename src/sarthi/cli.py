"""Command-line entry point: ``sarthi <command>``."""

from __future__ import annotations

import typer

from sarthi import __version__
from sarthi.config import get_settings
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
