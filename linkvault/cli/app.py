"""Typer CLI — placeholder for Item 7."""
from __future__ import annotations

import typer

app = typer.Typer(name="linkvault", help="LinkVault CLI client.")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())

