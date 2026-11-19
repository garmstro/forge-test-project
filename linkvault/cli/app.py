"""Typer CLI client for a running LinkVault server (AGENTS.md, Item 7)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, NoReturn

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table

DEFAULT_API_URL = "http://localhost:8000"
DESTINATION_MAX_CHARS = 50

app = typer.Typer(
    name="linkvault",
    help="LinkVault CLI client.",
    no_args_is_help=True,
)
console = Console()

ApiUrlOption = Annotated[
    str,
    typer.Option(
        "--api-url",
        envvar="LINKVAULT_API_URL",
        help="Base URL of the LinkVault server.",
        show_default=True,
    ),
]


# ---------------------------------------------------------------------------
# Config storage (~/.linkvault/config.json)
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    base = os.environ.get("LINKVAULT_CONFIG_DIR")
    directory = Path(base) if base else Path.home() / ".linkvault"
    return directory / "config.json"


def _load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_config(data: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _make_client(api_url: str) -> httpx.Client:
    """Build the HTTP client (separate function so tests can substitute it)."""
    return httpx.Client(base_url=api_url.rstrip("/"), timeout=10.0)


def _fail(message: str) -> NoReturn:
    console.print(f"[red]{escape(message)}[/red]")
    raise typer.Exit(code=1)


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text or response.reason_phrase}"
    if isinstance(body, dict):
        detail = body.get("detail")
        error = body.get("error")
        if isinstance(detail, list):  # FastAPI's default validation format
            detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
        if error and detail:
            return f"{error}: {detail}"
        if detail or error:
            return str(detail or error)
    return f"HTTP {response.status_code}"


def _request(
    method: str,
    path: str,
    api_url: str,
    *,
    auth: bool = True,
    **kwargs: Any,
) -> Any:
    """Perform a request; print the API error in red and exit(1) on failure."""
    headers: dict[str, str] = {}
    if auth:
        api_key = _load_config().get("api_key")
        if not api_key:
            _fail("Not logged in. Run `linkvault login` first.")
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with _make_client(api_url) as client:
            response = client.request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        _fail(f"Could not reach {api_url}: {exc}")
    if response.status_code >= 400:
        _fail(_error_message(response))
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _link_status(link: dict[str, Any]) -> str:
    if link.get("deleted_at"):
        return "deleted"
    expires = _parse_dt(link.get("expires_at"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if expires is not None and expires < now:
        return "expired"
    max_clicks = link.get("max_clicks")
    if max_clicks is not None and link.get("click_count", 0) >= max_clicks:
        return "exhausted"
    return "active"


def _truncate(text: str, limit: int = DESTINATION_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def login(
    api_url: ApiUrlOption = DEFAULT_API_URL,
    email: Annotated[str | None, typer.Option(help="Account email (prompted if omitted).")] = None,
    password: Annotated[
        str | None,
        typer.Option(help="Account password (prompted if omitted).", hide_input=True),
    ] = None,
) -> None:
    """Log in and store the API key in ~/.linkvault/config.json."""
    if email is None:
        email = typer.prompt("Email")
    if password is None:
        password = typer.prompt("Password", hide_input=True)
    data = _request(
        "POST", "/users/token", api_url, auth=False, json={"email": email, "password": password}
    )
    _save_config({"api_key": data["api_key"], "email": email})
    console.print(f"[green]Logged in as {escape(email)}.[/green]")


@app.command()
def logout() -> None:
    """Remove stored credentials."""
    path = _config_path()
    if path.exists():
        path.unlink()
        console.print("Logged out.")
    else:
        console.print("Not logged in.")


@app.command()
def shorten(
    url: Annotated[str, typer.Argument(help="Destination URL (http or https).")],
    slug: Annotated[str | None, typer.Option(help="Custom slug (auto-generated if omitted).")] = None,
    expires: Annotated[
        str | None, typer.Option(help="Expiry date, YYYY-MM-DD (end of day, UTC).")
    ] = None,
    max_clicks: Annotated[
        int | None, typer.Option("--max-clicks", help="Deactivate after N clicks.")
    ] = None,
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Create a short link."""
    payload: dict[str, Any] = {"url": url}
    if slug:
        payload["slug"] = slug
    if expires:
        try:
            day = datetime.strptime(expires, "%Y-%m-%d")
        except ValueError:
            _fail("--expires must be in YYYY-MM-DD format.")
        payload["expires_at"] = day.strftime("%Y-%m-%dT23:59:59Z")
    if max_clicks is not None:
        payload["max_clicks"] = max_clicks
    link = _request("POST", "/links", api_url, json=payload)
    console.print(f"[bold green]{escape(api_url.rstrip('/') + '/' + link['slug'])}[/bold green]", highlight=False)


@app.command("list")
def list_links(
    page: Annotated[int, typer.Option(min=1, help="Page number.")] = 1,
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """List your links."""
    data = _request("GET", "/links", api_url, params={"page": page})
    table = Table(title=f"Links (page {data['page']}, {data['total']} total)")
    table.add_column("Slug", style="cyan")
    table.add_column("Destination")
    table.add_column("Clicks", justify="right")
    table.add_column("Expires")
    table.add_column("Status")
    for link in data["items"]:
        expires = _parse_dt(link.get("expires_at"))
        table.add_row(
            link["slug"],
            _truncate(link["destination_url"]),
            str(link["click_count"]),
            expires.strftime("%Y-%m-%d %H:%M") if expires else "never",
            _link_status(link),
        )
    console.print(table)


@app.command()
def info(
    slug: Annotated[str, typer.Argument(help="Slug of the link.")],
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Show full metadata for a link."""
    link = _request("GET", f"/links/{slug}", api_url)
    table = Table(show_header=False, title=f"Link: {link['slug']}")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Short URL", api_url.rstrip("/") + "/" + link["slug"])
    table.add_row("Destination", link["destination_url"])
    table.add_row("Clicks", str(link["click_count"]))
    table.add_row("Max clicks", str(link["max_clicks"]) if link.get("max_clicks") is not None else "unlimited")
    table.add_row("Expires", link.get("expires_at") or "never")
    table.add_row("Created", link["created_at"])
    table.add_row("Status", _link_status(link))
    console.print(table)


@app.command()
def stats(
    slug: Annotated[str, typer.Argument(help="Slug of the link.")],
    days: Annotated[int, typer.Option(min=1, max=365, help="Lookback window in days.")] = 30,
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Show click analytics for a link, with a clicks-by-day bar chart."""
    data = _request("GET", f"/analytics/{slug}", api_url, params={"days": days})
    console.print(
        f"[bold]{escape(data['slug'])}[/bold] — {data['total_clicks']} clicks, "
        f"{data['unique_ips']} unique IPs (last {days} days)"
    )
    by_day = data["clicks_by_day"]
    if by_day:
        peak = max(d["clicks"] for d in by_day) or 1
        with Progress(
            TextColumn("{task.description}"),
            BarColumn(bar_width=40),
            TextColumn("{task.completed:.0f}"),
            console=console,
        ) as progress:
            for entry in by_day:
                progress.add_task(entry["date"], total=peak, completed=entry["clicks"])
    else:
        console.print("No clicks in this period.")
    if data["top_referers"]:
        table = Table(title="Top referers")
        table.add_column("Referer")
        table.add_column("Clicks", justify="right")
        for ref in data["top_referers"]:
            table.add_row(ref["referer"], str(ref["clicks"]))
        console.print(table)


@app.command()
def delete(
    slug: Annotated[str, typer.Argument(help="Slug of the link.")],
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Soft-delete a link."""
    _request("DELETE", f"/links/{slug}", api_url)
    console.print(f"Deleted [cyan]{escape(slug)}[/cyan].")
