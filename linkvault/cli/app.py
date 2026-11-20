"""LinkVault command-line client.

The CLI talks to a running LinkVault server over HTTP.  The server address is
taken from ``--api-url``, then the ``LINKVAULT_API_URL`` environment variable,
then ``http://localhost:8000``.  Credentials obtained with ``linkvault login``
are stored in ``~/.linkvault/config.json`` (readable by the current user only).
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, NoReturn

import httpx
import typer
from rich.console import Console
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

DEFAULT_API_URL = "http://localhost:8000"
LIST_PAGE_SIZE = 20
DESTINATION_MAX_CHARS = 50
REQUEST_TIMEOUT_SECONDS = 10.0

# Importing the ``linkvault`` package configures INFO-level logging for the
# server; keep httpx's per-request log lines out of the CLI's output.
logging.getLogger("httpx").setLevel(logging.WARNING)

app = typer.Typer(
    name="linkvault",
    help="LinkVault CLI client — shorten URLs and inspect click analytics.",
    no_args_is_help=False,
)

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


def _out() -> Console:
    """Console for normal output (created per call so it follows sys.stdout)."""
    return Console(highlight=False)


def _err() -> Console:
    """Console for error output on stderr."""
    return Console(stderr=True, highlight=False)


def _fail(message: str, hint: str | None = None) -> NoReturn:
    """Print *message* in red on stderr and exit with status 1."""
    console = _err()
    console.print(Text(f"Error: {message}", style="bold red"), soft_wrap=True)
    if hint:
        console.print(Text(hint, style="red"), soft_wrap=True)
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    return Path.home() / ".linkvault"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict[str, Any]:
    """Return the stored config, or an empty dict if missing or unreadable."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict[str, Any]) -> None:
    """Write *data* to the config file, readable only by the current user."""
    directory = config_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = config_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(path, 0o600)


def _require_api_key() -> str:
    api_key = load_config().get("api_key")
    if not isinstance(api_key, str) or not api_key:
        _fail("You are not logged in.", "Run `linkvault login` first.")
    return api_key


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _normalize_url(api_url: str) -> str:
    return api_url.strip().rstrip("/") or DEFAULT_API_URL


def build_client(api_url: str, api_key: str | None) -> httpx.Client:
    """Create the HTTP client used for every request (overridable in tests)."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.Client(
        base_url=api_url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
    )


def api_error_message(response: httpx.Response) -> str:
    """Extract a human-readable error message from an API error response.

    Handles the LinkVault envelope (``{"error": ..., "detail": ...}``), plain
    FastAPI errors (``{"detail": "..."}`` or a list of validation errors), the
    redirect engine's ``{"error": "not_found"}`` and non-JSON bodies.
    """
    try:
        body: Any = response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        detail = body.get("detail")
        error = body.get("error")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, list) and detail:
            parts = [
                str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                for item in detail
            ]
            return "; ".join(parts)
        if isinstance(error, str) and error.strip():
            return error.strip().replace("_", " ").capitalize()
        if detail is not None:
            return json.dumps(detail)

    text = response.text.strip()
    if text:
        return text[:200]
    return response.reason_phrase or f"HTTP {response.status_code}"


def _request(
    method: str,
    path: str,
    *,
    api_url: str,
    api_key: str | None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send a request and return the response, or print a red error and exit."""
    try:
        with build_client(api_url, api_key) as client:
            response = client.request(method, path, json=json_body, params=params)
    except httpx.TimeoutException:
        _fail(f"The LinkVault server at {api_url} did not respond in time.")
    except httpx.RequestError:
        _fail(
            f"Could not connect to the LinkVault server at {api_url}.",
            "Is the server running? Set --api-url or LINKVAULT_API_URL to change the address.",
        )

    if response.is_error:
        message = api_error_message(response)
        hint = None
        if response.status_code == 401 and api_key:
            hint = "Your API key is no longer valid. Run `linkvault login` again."
        _fail(f"{message} (HTTP {response.status_code})", hint)
    return response


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        _fail("The server returned an unexpected (non-JSON) response.")
    if not isinstance(data, dict):
        _fail("The server returned an unexpected response.")
    return data


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from the API; naive values are UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_dt(value: Any) -> str:
    dt = _parse_dt(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


def link_status(link: dict[str, Any], now: datetime | None = None) -> str:
    """Return ``active``, ``expired`` or ``used up`` for a link payload."""
    now = now or datetime.now(timezone.utc)
    expires_at = _parse_dt(link.get("expires_at"))
    if expires_at is not None and expires_at < now:
        return "expired"
    max_clicks = link.get("max_clicks")
    if isinstance(max_clicks, int) and int(link.get("click_count") or 0) >= max_clicks:
        return "used up"
    return "active"


_STATUS_STYLES = {"active": "green", "expired": "red", "used up": "yellow"}


def _status_text(status: str) -> Text:
    return Text(status, style=_STATUS_STYLES.get(status, ""))


def truncate(value: str, limit: int = DESTINATION_MAX_CHARS) -> str:
    """Cut *value* to at most *limit* characters, ending in '...' if cut."""
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _parse_expiry_date(value: str) -> str:
    """Turn ``YYYY-MM-DD`` into an end-of-day UTC ISO timestamp."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        _fail(
            f"Invalid --expires date {value!r}.",
            "Use the format YYYY-MM-DD, e.g. 2026-12-31.",
        )
    return f"{day.isoformat()}T23:59:59Z"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """LinkVault CLI client — shorten URLs and inspect click analytics."""
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
    """Log in with email and password and store the API key locally.

    Logging in issues a new API key and replaces the previous one, so other
    devices using the old key are signed out.
    """
    api_url = _normalize_url(api_url)
    response = _request(
        "POST",
        "/users/token",
        api_url=api_url,
        api_key=None,
        json_body={"email": email, "password": password},
    )
    api_key = _json(response).get("api_key")
    if not isinstance(api_key, str) or not api_key:
        _fail("The server did not return an API key.")

    save_config({"api_key": api_key, "email": email, "api_url": api_url})
    out = _out()
    out.print(Text(f"Logged in as {email}.", style="bold green"))
    out.print(f"API key saved to {config_path()}.", soft_wrap=True)
    out.print(
        Text(
            "Note: logging in issues a new API key and replaces the previous one, "
            "so any other devices using the old key are now signed out.",
            style="yellow",
        )
    )


@app.command()
def logout() -> None:
    """Remove the stored credentials from this computer."""
    path = config_path()
    if not path.exists():
        _out().print("You are not logged in — nothing to do.")
        return
    try:
        path.unlink()
    except OSError as exc:
        _fail(f"Could not remove {path}: {exc}")
    _out().print(Text("Logged out. Stored credentials removed.", style="green"))


@app.command()
def shorten(
    url: Annotated[str, typer.Argument(help="Destination URL (http:// or https://).")],
    slug: Annotated[
        str | None, typer.Option("--slug", help="Custom slug (3–64 chars).")
    ] = None,
    expires: Annotated[
        str | None,
        typer.Option(
            "--expires",
            metavar="YYYY-MM-DD",
            help="Expiry date; the link stops working after this day (UTC).",
        ),
    ] = None,
    max_clicks: Annotated[
        int | None,
        typer.Option("--max-clicks", min=1, help="Deactivate after N clicks."),
    ] = None,
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Create a short link and print its full URL."""
    api_url = _normalize_url(api_url)
    payload: dict[str, Any] = {"url": url}
    if slug is not None:
        payload["slug"] = slug
    if expires is not None:
        payload["expires_at"] = _parse_expiry_date(expires)
    if max_clicks is not None:
        payload["max_clicks"] = max_clicks

    api_key = _require_api_key()
    response = _request(
        "POST", "/links", api_url=api_url, api_key=api_key, json_body=payload
    )
    created_slug = _json(response).get("slug")
    if not isinstance(created_slug, str):
        _fail("The server did not return a slug.")
    _out().print(Text(f"{api_url}/{created_slug}", style="bold green"), soft_wrap=True)


@app.command("list")
def list_links(
    page: Annotated[int, typer.Option("--page", min=1, help="Page number.")] = 1,
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """List your short links in a table."""
    api_url = _normalize_url(api_url)
    api_key = _require_api_key()
    response = _request(
        "GET",
        "/links",
        api_url=api_url,
        api_key=api_key,
        params={"page": page, "page_size": LIST_PAGE_SIZE},
    )
    data = _json(response)
    items: list[dict[str, Any]] = data.get("items") or []
    total = int(data.get("total") or 0)
    page_size = int(data.get("page_size") or LIST_PAGE_SIZE)
    total_pages = max(1, math.ceil(total / page_size))
    out = _out()

    if not items:
        if total == 0:
            out.print("You have no links yet. Create one with `linkvault shorten <url>`.")
        else:
            out.print(
                f"Page {page} is empty — you have {total} link(s) across "
                f"{total_pages} page(s)."
            )
        return

    table = Table(title="Your links")
    table.add_column("Slug", style="cyan", no_wrap=True)
    table.add_column("Destination", no_wrap=True)
    table.add_column("Clicks", justify="right")
    table.add_column("Expires", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    for link in items:
        table.add_row(
            str(link.get("slug", "")),
            truncate(str(link.get("destination_url", ""))),
            str(link.get("click_count", 0)),
            _format_dt(link.get("expires_at")),
            _status_text(link_status(link)),
        )
    out.print(table)
    out.print(f"Page {page} of {total_pages} · {total} link(s) total")


@app.command()
def info(
    slug: Annotated[str, typer.Argument(help="Slug of the link.")],
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Show all details of one link, including its live click count."""
    api_url = _normalize_url(api_url)
    api_key = _require_api_key()
    link = _json(_request("GET", f"/links/{slug}", api_url=api_url, api_key=api_key))

    max_clicks = link.get("max_clicks")
    table = Table(show_header=False, title=f"Link '{link.get('slug', slug)}'")
    table.add_column("Field", style="bold")
    table.add_column("Value", overflow="fold")
    table.add_row("Slug", str(link.get("slug", slug)))
    table.add_row("Short URL", f"{api_url}/{link.get('slug', slug)}")
    table.add_row("Destination", str(link.get("destination_url", "")))
    table.add_row("Clicks", str(link.get("click_count", 0)))
    table.add_row("Max clicks", str(max_clicks) if max_clicks is not None else "—")
    table.add_row("Expires", _format_dt(link.get("expires_at")))
    table.add_row("Created", _format_dt(link.get("created_at")))
    table.add_row("Status", _status_text(link_status(link)))
    _out().print(table)


@app.command()
def stats(
    slug: Annotated[str, typer.Argument(help="Slug of the link.")],
    days: Annotated[
        int, typer.Option("--days", min=1, max=365, help="Lookback window in days.")
    ] = 30,
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Show click analytics for a link, with a clicks-per-day bar chart."""
    api_url = _normalize_url(api_url)
    api_key = _require_api_key()
    data = _json(
        _request(
            "GET",
            f"/analytics/{slug}",
            api_url=api_url,
            api_key=api_key,
            params={"days": days},
        )
    )
    out = _out()
    out.print(
        Text(f"Analytics for '{data.get('slug', slug)}' — last {days} day(s)", style="bold")
    )
    out.print(f"Total clicks: {data.get('total_clicks', 0)}")
    out.print(f"Unique visitors (IPs): {data.get('unique_ips', 0)}")

    by_day: list[dict[str, Any]] = data.get("clicks_by_day") or []
    peak = max((int(d.get("clicks") or 0) for d in by_day), default=0)
    chart = Table(title="Clicks by day", show_edge=False)
    chart.add_column("Date", no_wrap=True)
    chart.add_column("", no_wrap=True)
    chart.add_column("Clicks", justify="right")
    for day in by_day:
        clicks = int(day.get("clicks") or 0)
        chart.add_row(
            str(day.get("date", "")),
            ProgressBar(
                total=max(peak, 1),
                completed=clicks,
                width=40,
                complete_style="green",
                finished_style="green",
            ),
            str(clicks),
        )
    out.print(chart)

    referers: list[dict[str, Any]] = data.get("top_referers") or []
    ref_table = Table(title="Top referrers")
    ref_table.add_column("Referrer", overflow="fold")
    ref_table.add_column("Clicks", justify="right")
    for row in referers:
        ref_table.add_row(str(row.get("referer") or "(direct)"), str(row.get("clicks", 0)))
    if referers:
        out.print(ref_table)
    else:
        out.print("Top referrers: none yet")

    agents: list[dict[str, Any]] = data.get("top_user_agents") or []
    ua_table = Table(title="Top browsers")
    ua_table.add_column("User agent", overflow="fold")
    ua_table.add_column("Clicks", justify="right")
    for row in agents:
        ua_table.add_row(
            str(row.get("user_agent") or "(unknown)"), str(row.get("clicks", 0))
        )
    if agents:
        out.print(ua_table)
    else:
        out.print("Top browsers: none yet")


@app.command()
def delete(
    slug: Annotated[str, typer.Argument(help="Slug of the link to delete.")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Delete without asking for confirmation.")
    ] = False,
    api_url: ApiUrlOption = DEFAULT_API_URL,
) -> None:
    """Delete a link. Its slug can be reused afterwards."""
    api_url = _normalize_url(api_url)
    api_key = _require_api_key()
    if not yes and not typer.confirm(
        f"Delete link '{slug}'? Its slug will become available for reuse."
    ):
        _out().print("Cancelled — nothing was deleted.")
        return
    _request("DELETE", f"/links/{slug}", api_url=api_url, api_key=api_key)
    _out().print(Text(f"Deleted link '{slug}'.", style="green"))
