"""LinkVault command-line client (Typer + Rich).

Talks to a running LinkVault server. The server URL comes from ``--api-url``
or the ``LINKVAULT_API_URL`` environment variable, then the URL saved at
login, then ``http://localhost:8000``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Any, NoReturn, Optional

import typer
from rich.console import Console
from rich.progress_bar import ProgressBar
from rich.table import Table

from linkvault.cli import config
from linkvault.cli.client import ApiError, LinkVaultClient

app = typer.Typer(
    name="linkvault",
    help="LinkVault CLI client.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

DESTINATION_MAX = 50

ApiUrlOption = Annotated[
    Optional[str],
    typer.Option(
        "--api-url",
        envvar="LINKVAULT_API_URL",
        help="LinkVault server URL (default: saved login URL or http://localhost:8000).",
        show_default=False,
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fail(message: str) -> NoReturn:
    err_console.print(f"[bold red]Error:[/bold red] [red]{message}[/red]", highlight=False)
    raise typer.Exit(code=1)


def make_client(base_url: str, api_key: str | None) -> LinkVaultClient:
    """Build the API client. Tests replace this to inject a mock transport."""
    return LinkVaultClient(base_url, api_key=api_key)


def _authed_client(api_url: str | None) -> tuple[LinkVaultClient, str]:
    api_key = config.get_api_key()
    if api_key is None:
        _fail("Not logged in. Run `linkvault login` first.")
    base_url = config.resolve_api_url(api_url)
    return make_client(base_url, api_key), base_url


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # API stores UTC (DECISIONS.md §4)
    return parsed


def _status(link: dict[str, Any], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    expires = _parse_dt(link.get("expires_at"))
    if expires is not None and expires < now:
        return "expired"
    max_clicks = link.get("max_clicks")
    if max_clicks is not None and int(link.get("click_count", 0)) >= int(max_clicks):
        return "exhausted"
    return "active"


_STATUS_STYLE = {"active": "green", "expired": "red", "exhausted": "yellow"}


def _styled_status(status: str) -> str:
    return f"[{_STATUS_STYLE.get(status, 'white')}]{status}[/]"


def _truncate(text: str, limit: int = DESTINATION_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_dt(value: Any, empty: str = "never") -> str:
    parsed = _parse_dt(value)
    return parsed.strftime("%Y-%m-%d %H:%M UTC") if parsed else empty


def _expires_to_iso(value: str) -> str:
    """Convert --expires YYYY-MM-DD to the end of that day in UTC."""
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter("Use the format YYYY-MM-DD.", param_hint="--expires")
    return f"{day.isoformat()}T23:59:59Z"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def login(
    email: Annotated[str, typer.Option(prompt=True, help="Account email.")],
    password: Annotated[
        str, typer.Option(prompt=True, hide_input=True, help="Account password.")
    ],
    api_url: ApiUrlOption = None,
) -> None:
    """Log in and store an API key in ~/.linkvault/config.json.

    Note: the server issues a new API key on every login, which invalidates
    any key issued earlier for this account.
    """
    base_url = config.resolve_api_url(api_url)
    try:
        with make_client(base_url, None) as client:
            result = client.token(email, password)
    except ApiError as exc:
        _fail(exc.message)
    path = config.save_config(
        {"api_url": base_url, "email": email, "api_key": result["api_key"]}
    )
    console.print(f"[green]Logged in as[/green] [bold]{email}[/bold] ({base_url}).")
    console.print(f"[dim]Credentials saved to {path}[/dim]", highlight=False)


@app.command()
def logout() -> None:
    """Remove stored credentials."""
    if config.clear_config():
        console.print("[green]Logged out.[/green] Stored credentials removed.")
    else:
        console.print("[yellow]Not logged in;[/yellow] nothing to remove.")


@app.command()
def shorten(
    url: Annotated[str, typer.Argument(help="Destination URL (http:// or https://).")],
    slug: Annotated[
        Optional[str], typer.Option("--slug", help="Custom slug (3–64 chars).")
    ] = None,
    expires: Annotated[
        Optional[str],
        typer.Option("--expires", help="Expiry date YYYY-MM-DD (end of day, UTC)."),
    ] = None,
    max_clicks: Annotated[
        Optional[int],
        typer.Option("--max-clicks", min=1, help="Deactivate after N clicks."),
    ] = None,
    api_url: ApiUrlOption = None,
) -> None:
    """Create a short link and print its URL."""
    expires_at = _expires_to_iso(expires) if expires else None
    client, base_url = _authed_client(api_url)
    try:
        with client:
            link = client.create_link(
                url, slug=slug, expires_at=expires_at, max_clicks=max_clicks
            )
    except ApiError as exc:
        _fail(exc.message)
    console.print(f"[bold green]{base_url}/{link['slug']}[/bold green]", highlight=False)


@app.command("list")
def list_links(
    page: Annotated[int, typer.Option("--page", min=1, help="Page number.")] = 1,
    api_url: ApiUrlOption = None,
) -> None:
    """List your links in a table."""
    client, _ = _authed_client(api_url)
    try:
        with client:
            data = client.list_links(page=page)
    except ApiError as exc:
        _fail(exc.message)

    items: list[dict[str, Any]] = data.get("items", [])
    total = int(data.get("total", 0))
    page_size = int(data.get("page_size", 20)) or 20
    pages = max(1, -(-total // page_size))

    if not items:
        message = "You have no links yet." if total == 0 else f"No links on page {page}."
        console.print(f"[yellow]{message}[/yellow] Create one with `linkvault shorten <url>`.")
        return

    table = Table(title=f"Your links — page {page} of {pages} ({total} total)")
    table.add_column("Slug", style="bold cyan", no_wrap=True)
    table.add_column("Destination", overflow="fold")
    table.add_column("Clicks", justify="right")
    table.add_column("Expires", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    for link in items:
        clicks = str(link.get("click_count", 0))
        if link.get("max_clicks") is not None:
            clicks += f" / {link['max_clicks']}"
        table.add_row(
            link["slug"],
            _truncate(str(link.get("destination_url", ""))),
            clicks,
            _fmt_dt(link.get("expires_at")),
            _styled_status(_status(link)),
        )
    console.print(table)


@app.command()
def info(
    slug: Annotated[str, typer.Argument(help="Slug of the link.")],
    api_url: ApiUrlOption = None,
) -> None:
    """Show full details for one link."""
    client, base_url = _authed_client(api_url)
    try:
        with client:
            link = client.get_link(slug)
    except ApiError as exc:
        _fail(exc.message)

    max_clicks = link.get("max_clicks")
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Short URL", f"[bold green]{base_url}/{link['slug']}[/bold green]")
    table.add_row("Destination", str(link.get("destination_url", "")))
    table.add_row("Clicks", str(link.get("click_count", 0)))
    table.add_row("Max clicks", str(max_clicks) if max_clicks is not None else "unlimited")
    table.add_row("Expires", _fmt_dt(link.get("expires_at")))
    table.add_row("Created", _fmt_dt(link.get("created_at"), empty="unknown"))
    table.add_row("Status", _styled_status(_status(link)))
    console.print(table)


@app.command()
def stats(
    slug: Annotated[str, typer.Argument(help="Slug of the link.")],
    days: Annotated[
        int, typer.Option("--days", min=1, max=365, help="Lookback window in days.")
    ] = 30,
    api_url: ApiUrlOption = None,
) -> None:
    """Show click analytics with a per-day bar chart."""
    client, _ = _authed_client(api_url)
    try:
        with client:
            data = client.link_stats(slug, days=days)
    except ApiError as exc:
        _fail(exc.message)

    console.print(
        f"[bold]{data['slug']}[/bold] — last {days} days: "
        f"[cyan]{data.get('total_clicks', 0)}[/cyan] clicks, "
        f"[cyan]{data.get('unique_ips', 0)}[/cyan] unique IPs",
        highlight=False,
    )

    by_day: list[dict[str, Any]] = data.get("clicks_by_day", [])
    if not by_day:
        console.print("[yellow]No clicks in this period.[/yellow]")
    else:
        peak = max(int(d["clicks"]) for d in by_day) or 1
        chart = Table(title="Clicks by day", box=None, padding=(0, 1))
        chart.add_column("Date", style="bold", no_wrap=True)
        chart.add_column("", ratio=1, min_width=10)
        chart.add_column("Clicks", justify="right")
        for day in by_day:
            clicks = int(day["clicks"])
            bar = ProgressBar(total=peak, completed=clicks, complete_style="green")
            chart.add_row(str(day["date"]), bar, str(clicks))
        console.print(chart)

    for key, title, header, field in (
        ("top_referers", "Top referers", "Referer", "referer"),
        ("top_user_agents", "Top user agents", "User agent", "user_agent"),
    ):
        rows: list[dict[str, Any]] = data.get(key, [])
        if not rows:
            continue
        table = Table(title=title)
        table.add_column(header, overflow="fold")
        table.add_column("Clicks", justify="right")
        for row in rows:
            table.add_row(_truncate(str(row.get(field, "")), 80), str(row.get("clicks", 0)))
        console.print(table)


@app.command()
def delete(
    slug: Annotated[str, typer.Argument(help="Slug of the link.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
    api_url: ApiUrlOption = None,
) -> None:
    """Soft-delete a link (its slug becomes reusable)."""
    if not yes:
        typer.confirm(f"Delete link '{slug}'?", abort=True)
    client, _ = _authed_client(api_url)
    try:
        with client:
            client.delete_link(slug)
    except ApiError as exc:
        _fail(exc.message)
    console.print(f"[green]Deleted[/green] [bold]{slug}[/bold].")


if __name__ == "__main__":  # pragma: no cover
    app()
