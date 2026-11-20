"""LinkVault command-line client (spec item 7).

The CLI is a thin HTTP client: every command talks to a running LinkVault
server through its REST API and never touches the database directly.

Server address resolution (highest priority first):

1. ``--api-url`` flag on the command
2. ``LINKVAULT_API_URL`` environment variable
3. ``http://localhost:8000``

Credentials are stored in ``~/.linkvault/config.json`` (file mode 0600).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, NoReturn
from urllib.parse import quote

import httpx
import typer
from rich.console import Console
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

DEFAULT_API_URL = "http://localhost:8000"
REQUEST_TIMEOUT_SECONDS = 10.0
DESTINATION_MAX_CHARS = 50
BAR_WIDTH = 40

app = typer.Typer(
    name="linkvault",
    help="LinkVault CLI client — shorten links and inspect their analytics.",
    no_args_is_help=False,
    # Never print local variables in tracebacks: they may contain the API key.
    pretty_exceptions_show_locals=False,
)

# Importing the ``linkvault`` package loads the server module, which enables
# INFO-level logging; keep httpx's per-request log lines out of CLI output.
for _noisy_logger in ("httpx", "httpcore"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

console = Console()
err_console = Console(stderr=True)

# Tests replace this with an ``httpx.MockTransport`` so no real server is needed.
_transport: httpx.BaseTransport | None = None

ApiUrlOption = Annotated[
    str,
    typer.Option(
        "--api-url",
        envvar="LINKVAULT_API_URL",
        help="Base URL of the LinkVault server.",
    ),
]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _fail(message: str) -> NoReturn:
    """Print *message* in red to stderr and exit with status 1."""
    err_console.print(Text(message, style="bold red"))
    raise typer.Exit(code=1)


def _success(message: str) -> None:
    console.print(Text(message, style="green"))


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    return Path.home() / ".linkvault"


def config_path() -> Path:
    return config_dir() / "config.json"


def _save_api_key(api_key: str) -> Path:
    directory = config_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = config_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"api_key": api_key}, fh)
    # O_CREAT's mode is ignored for pre-existing files, so enforce it explicitly.
    os.chmod(path, 0o600)
    return path


def _load_api_key() -> str:
    """Return the stored API key, or exit with a hint to log in."""
    path = config_path()
    if not path.exists():
        _fail("You are not logged in. Run `linkvault login` first.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _fail(f"Could not read {path}. Run `linkvault login` first.")
    api_key = data.get("api_key") if isinstance(data, dict) else None
    if not isinstance(api_key, str) or not api_key:
        _fail("You are not logged in. Run `linkvault login` first.")
    return api_key


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _error_message(response: httpx.Response) -> str:
    """Extract a human-readable message from an API error response.

    The server uses two shapes: ``{"detail": "..."}`` (FastAPI HTTPException)
    and ``{"error": "code", "detail": "..."}`` (custom envelope). Redirect
    errors use ``{"error": "code"}`` alone.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, list) and detail:
            # FastAPI's default validation format: [{"loc": [...], "msg": "..."}]
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    loc = ".".join(str(p) for p in item.get("loc", []))
                    msg = str(item.get("msg", "invalid value"))
                    parts.append(f"{loc}: {msg}" if loc else msg)
                else:
                    parts.append(str(item))
            return "; ".join(parts)
        error = body.get("error")
        if isinstance(error, str) and error:
            return error
    text = response.text.strip()
    if text and len(text) <= 200:
        return text
    return f"HTTP {response.status_code} {response.reason_phrase}".strip()


def _request(
    method: str,
    path: str,
    api_url: str,
    *,
    api_key: str | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send a request and return the response, or print a red error and exit."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    base_url = api_url.rstrip("/")
    try:
        with httpx.Client(
            base_url=base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=_transport,
        ) as client:
            response = client.request(
                method, path, headers=headers, json=json_body, params=params
            )
    except (httpx.InvalidURL, httpx.UnsupportedProtocol):
        _fail(f"Invalid server address: {api_url}")
    except httpx.HTTPError:
        _fail(f"Could not connect to {base_url}. Is the server running?")

    if response.is_success:
        return response

    message = _error_message(response)
    if response.status_code == 401 and api_key is not None:
        message += " Your saved login may be invalid or expired; run `linkvault login` again."
    _fail(f"Error: {message}")


def _json(response: httpx.Response) -> dict[str, Any]:
    """Return the response body as a JSON object, or exit with a red error."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        _fail("The server returned an unexpected response.")
    return data


def _slug_path(prefix: str, slug: str) -> str:
    return f"{prefix}/{quote(slug, safe='')}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from the API; naive values are treated as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: Any, empty: str = "Never") -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return empty
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def link_status(link: dict[str, Any], now: datetime | None = None) -> str:
    """Return "Active", "Expired" (past its date) or "Exhausted" (click cap hit)."""
    now = now or datetime.now(timezone.utc)
    expires_at = _parse_datetime(link.get("expires_at"))
    if expires_at is not None and expires_at < now:
        return "Expired"
    max_clicks = link.get("max_clicks")
    if isinstance(max_clicks, int) and int(link.get("click_count") or 0) >= max_clicks:
        return "Exhausted"
    return "Active"


_STATUS_STYLES = {"Active": "green", "Expired": "red", "Exhausted": "yellow"}


def _status_text(status: str) -> Text:
    return Text(status, style=_STATUS_STYLES.get(status, ""))


def truncate(value: str, limit: int = DESTINATION_MAX_CHARS) -> str:
    """Cut *value* to at most *limit* characters, marking the cut with "…"."""
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _short_url(api_url: str, slug: str) -> str:
    return f"{api_url.rstrip('/')}/{slug}"


def _parse_expires(value: str) -> str:
    """Turn ``YYYY-MM-DD`` into the end of that day in UTC (ISO-8601)."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        _fail(f"Invalid --expires date '{value}'. Use the format YYYY-MM-DD.")
    return f"{day.isoformat()}T23:59:59Z"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """LinkVault CLI client — shorten links and inspect their analytics."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


@app.command()
def login(
    email: Annotated[
        str, typer.Option("--email", prompt=True, help="Account email address.")
    ],
    password: Annotated[
        str,
        typer.Option(
            "--password", prompt=True, hide_input=True, help="Account password."
        ),
    ],
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Log in and store your API key in ~/.linkvault/config.json.

    Logging in issues a new API key; any previously issued key stops working.
    """
    response = _request(
        "POST",
        "/users/token",
        api_url,
        json_body={"email": email, "password": password},
    )
    data = _json(response)
    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        _fail("The server did not return an API key.")
    try:
        path = _save_api_key(api_key)
    except OSError as exc:
        _fail(f"Could not save credentials: {exc}")
    _success(f"Logged in as {email}. API key saved to {path}.")


@app.command()
def logout() -> None:
    """Remove stored credentials."""
    path = config_path()
    if not path.exists():
        console.print("You are not logged in; nothing to do.")
        return
    try:
        path.unlink()
    except OSError as exc:
        _fail(f"Could not remove {path}: {exc}")
    _success("Logged out. Stored credentials removed.")


@app.command()
def shorten(
    url: Annotated[str, typer.Argument(help="Destination URL (http:// or https://).")],
    slug: Annotated[
        str | None, typer.Option("--slug", help="Custom slug (3-64 chars).")
    ] = None,
    expires: Annotated[
        str | None,
        typer.Option(
            "--expires",
            metavar="YYYY-MM-DD",
            help="Expiry date; the link expires at the end of that day (UTC).",
        ),
    ] = None,
    max_clicks: Annotated[
        int | None,
        typer.Option("--max-clicks", min=1, help="Deactivate after N clicks."),
    ] = None,
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Create a short link and print its full URL."""
    body: dict[str, Any] = {"url": url}
    if slug is not None:
        body["slug"] = slug
    if expires is not None:
        body["expires_at"] = _parse_expires(expires)
    if max_clicks is not None:
        body["max_clicks"] = max_clicks

    api_key = _load_api_key()
    data = _json(_request("POST", "/links", api_url, api_key=api_key, json_body=body))
    console.print(Text(_short_url(api_url, str(data.get("slug", ""))), style="bold green"))


@app.command("list")
def list_links(
    page: Annotated[int, typer.Option("--page", min=1, help="Page number.")] = 1,
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """List your links in a table."""
    api_key = _load_api_key()
    data = _json(
        _request("GET", "/links", api_url, api_key=api_key, params={"page": page})
    )
    items: list[dict[str, Any]] = data.get("items", [])
    total = int(data.get("total", 0))
    page_size = max(int(data.get("page_size", 20)), 1)
    pages = max((total + page_size - 1) // page_size, 1)

    if not items:
        if total == 0:
            console.print("You have no links yet. Create one with `linkvault shorten <url>`.")
        else:
            console.print(f"Page {page} is empty ({total} links in total, {pages} pages).")
        return

    table = Table(title="Your links")
    table.add_column("Slug", style="cyan", no_wrap=True)
    table.add_column("Destination", no_wrap=True, max_width=DESTINATION_MAX_CHARS)
    table.add_column("Clicks", justify="right")
    table.add_column("Expires", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    for link in items:
        table.add_row(
            str(link.get("slug", "")),
            truncate(str(link.get("destination_url", ""))),
            str(link.get("click_count", 0)),
            _format_datetime(link.get("expires_at")),
            _status_text(link_status(link)),
        )
    console.print(table)
    console.print(f"Page {data.get('page', page)} of {pages} — {total} links in total.")


@app.command()
def info(
    slug: Annotated[str, typer.Argument(help="Slug of the link.")],
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Show full details for one link, including its live click count."""
    api_key = _load_api_key()
    link = _json(_request("GET", _slug_path("/links", slug), api_url, api_key=api_key))

    max_clicks = link.get("max_clicks")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Slug", Text(str(link.get("slug", "")), style="cyan"))
    grid.add_row("Short URL", _short_url(api_url, str(link.get("slug", ""))))
    grid.add_row("Destination", Text(str(link.get("destination_url", ""))))
    grid.add_row("Status", _status_text(link_status(link)))
    grid.add_row("Clicks", str(link.get("click_count", 0)))
    grid.add_row("Max clicks", "Unlimited" if max_clicks is None else str(max_clicks))
    grid.add_row("Expires", _format_datetime(link.get("expires_at")))
    grid.add_row("Created", _format_datetime(link.get("created_at"), empty="Unknown"))
    console.print(grid)


@app.command()
def stats(
    slug: Annotated[str, typer.Argument(help="Slug of the link.")],
    days: Annotated[
        int, typer.Option("--days", min=1, max=365, help="Lookback window in days.")
    ] = 30,
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Show click analytics for a link, with a clicks-by-day bar chart."""
    api_key = _load_api_key()
    data = _json(
        _request(
            "GET",
            _slug_path("/analytics", slug),
            api_url,
            api_key=api_key,
            params={"days": days},
        )
    )

    console.print(
        Text.assemble(
            ("Analytics for ", "bold"),
            (str(data.get("slug", slug)), "bold cyan"),
            (f" (last {days} days)", "bold"),
        )
    )
    console.print(
        f"Total clicks: {data.get('total_clicks', 0)}    "
        f"Unique visitors: {data.get('unique_ips', 0)}"
    )

    by_day: list[dict[str, Any]] = data.get("clicks_by_day", [])
    console.print()
    console.print(Text("Clicks by day", style="bold underline"))
    if by_day:
        peak = max(int(d.get("clicks", 0)) for d in by_day)
        chart = Table.grid(padding=(0, 1))
        chart.add_column(no_wrap=True)
        chart.add_column(width=BAR_WIDTH)
        chart.add_column(justify="right", no_wrap=True)
        for day in by_day:
            clicks = int(day.get("clicks", 0))
            chart.add_row(
                str(day.get("date", "")),
                ProgressBar(
                    total=max(peak, 1),
                    completed=clicks,
                    width=BAR_WIDTH,
                    complete_style="green",
                    finished_style="green",
                ),
                str(clicks),
            )
        console.print(chart)
    else:
        console.print("No click data.")

    for title, key, label in (
        ("Top referers", "top_referers", "referer"),
        ("Top user agents", "top_user_agents", "user_agent"),
    ):
        rows: list[dict[str, Any]] = data.get(key, [])
        console.print()
        if not rows:
            console.print(Text(f"{title}: none", style="bold"))
            continue
        table = Table(title=title, title_justify="left")
        table.add_column(label.replace("_", " ").title(), overflow="fold")
        table.add_column("Clicks", justify="right")
        for row in rows:
            table.add_row(Text(str(row.get(label, ""))), str(row.get("clicks", 0)))
        console.print(table)


@app.command()
def delete(
    slug: Annotated[str, typer.Argument(help="Slug of the link to delete.")],
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Delete a link. Its slug becomes available for reuse."""
    api_key = _load_api_key()
    _request("DELETE", _slug_path("/links", slug), api_url, api_key=api_key)
    _success(f"Deleted link '{slug}'.")


if __name__ == "__main__":  # pragma: no cover
    app()
