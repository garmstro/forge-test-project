"""Typer CLI — full implementation of LinkVault client."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(name="linkvault", help="LinkVault CLI client.")
console = Console()

# Configuration file location
CONFIG_DIR = Path.home() / ".linkvault"
CONFIG_FILE = CONFIG_DIR / "config.json"


def get_api_url(api_url: Optional[str] = None) -> str:
    """Get API URL from flag, environment, or default."""
    if api_url:
        return api_url
    return os.getenv("LINKVAULT_API_URL", "http://localhost:8000")


def load_config() -> dict:
    """Load credentials from config file."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_config(config: dict) -> None:
    """Save credentials to config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    CONFIG_FILE.chmod(0o600)  # Restrict to owner only


def get_api_key() -> str:
    """Get stored API key or raise error."""
    config = load_config()
    if "api_key" not in config:
        console.print(
            "[red]Error:[/red] Not logged in. Run [bold]linkvault login[/bold] first."
        )
        raise typer.Exit(1)
    return config["api_key"]


def get_headers(api_key: str) -> dict:
    """Build authorization headers."""
    return {"Authorization": f"Bearer {api_key}"}


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """LinkVault CLI — manage short links from the terminal."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


@app.command()
def login(
    email: str = typer.Option(..., prompt=True, help="Email address"),
    password: str = typer.Option(
        ..., prompt=True, hide_input=True, help="Password"
    ),
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API server URL (default: LINKVAULT_API_URL or http://localhost:8000)"
    ),
) -> None:
    """Log in and store API credentials."""
    url = get_api_url(api_url)

    try:
        with httpx.Client() as client:
            response = client.post(
                f"{url}/users/token",
                json={"email": email, "password": password},
            )
            response.raise_for_status()
            data = response.json()
            api_key = data["api_key"]

        config = load_config()
        config["email"] = email
        config["api_key"] = api_key
        config["api_url"] = url
        save_config(config)

        console.print(
            f"[green]✓[/green] Logged in as [bold]{email}[/bold]"
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            console.print("[red]Error:[/red] Invalid email or password.")
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except httpx.RequestError as e:
        console.print(f"[red]Error:[/red] Could not connect to {url}: {e}")
        raise typer.Exit(1)


@app.command()
def logout() -> None:
    """Log out and remove stored credentials."""
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
        console.print("[green]✓[/green] Logged out.")
    else:
        console.print("[yellow]Warning:[/yellow] Not logged in.")


@app.command()
def shorten(
    url: str = typer.Argument(..., help="URL to shorten"),
    slug: Optional[str] = typer.Option(
        None, "--slug", help="Custom slug (optional)"
    ),
    expires: Optional[str] = typer.Option(
        None, "--expires", help="Expiration date (YYYY-MM-DD)"
    ),
    max_clicks: Optional[int] = typer.Option(
        None, "--max-clicks", help="Maximum number of clicks"
    ),
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API server URL"
    ),
) -> None:
    """Create a new short link."""
    api_key = get_api_key()
    url_base = get_api_url(api_url)

    # Build payload
    payload: dict = {"url": url}
    if slug:
        payload["slug"] = slug
    if expires:
        try:
            # Parse and convert to ISO format with time
            dt = datetime.strptime(expires, "%Y-%m-%d")
            payload["expires_at"] = dt.isoformat() + "T00:00:00"
        except ValueError:
            console.print(
                "[red]Error:[/red] Invalid date format. Use YYYY-MM-DD."
            )
            raise typer.Exit(1)
    if max_clicks is not None:
        payload["max_clicks"] = max_clicks

    try:
        with httpx.Client() as client:
            response = client.post(
                f"{url_base}/links",
                json=payload,
                headers=get_headers(api_key),
            )
            response.raise_for_status()
            data = response.json()

        short_url = f"{url_base}/{data['slug']}"
        console.print(f"[green]✓[/green] Link created:")
        console.print(f"  Short URL: [bold green]{short_url}[/bold green]")
        console.print(f"  Slug: [bold]{data['slug']}[/bold]")
        console.print(f"  Destination: {data['destination_url']}")
        if data.get("expires_at"):
            console.print(f"  Expires: {data['expires_at']}")
        if data.get("max_clicks"):
            console.print(f"  Max clicks: {data['max_clicks']}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 422:
            detail = e.response.json().get("detail", "Invalid input")
            console.print(f"[red]Error:[/red] {detail}")
        elif e.response.status_code == 409:
            console.print(
                "[red]Error:[/red] Slug already in use or collision. Try another."
            )
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except httpx.RequestError as e:
        console.print(f"[red]Error:[/red] Could not connect to {url_base}: {e}")
        raise typer.Exit(1)


@app.command()
def list(
    page: int = typer.Option(1, "--page", help="Page number"),
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API server URL"
    ),
) -> None:
    """List your short links."""
    api_key = get_api_key()
    url_base = get_api_url(api_url)

    try:
        with httpx.Client() as client:
            response = client.get(
                f"{url_base}/links",
                params={"page": page, "page_size": 20},
                headers=get_headers(api_key),
            )
            response.raise_for_status()
            data = response.json()

        if not data["items"]:
            console.print("[yellow]No links found.[/yellow]")
            return

        table = Table(title=f"Your Links (Page {data['page']} of {(data['total'] + 19) // 20})")
        table.add_column("Slug", style="cyan", no_wrap=True)
        table.add_column("Destination", style="blue")
        table.add_column("Clicks", justify="right", style="magenta")
        table.add_column("Created", style="green")
        table.add_column("Expires", style="yellow")

        for link in data["items"]:
            expires = link.get("expires_at")
            if expires:
                expires = expires.split("T")[0]  # Just the date
            else:
                expires = "—"

            created = link["created_at"].split("T")[0]

            table.add_row(
                link["slug"],
                link["destination_url"][:50],
                str(link["click_count"]),
                created,
                expires,
            )

        console.print(table)
        console.print(
            f"\n[dim]Total: {data['total']} links[/dim]"
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except httpx.RequestError as e:
        console.print(f"[red]Error:[/red] Could not connect to {url_base}: {e}")
        raise typer.Exit(1)


@app.command()
def info(
    slug: str = typer.Argument(..., help="Link slug"),
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API server URL"
    ),
) -> None:
    """Show metadata for a single link."""
    api_key = get_api_key()
    url_base = get_api_url(api_url)

    try:
        with httpx.Client() as client:
            response = client.get(
                f"{url_base}/links/{slug}",
                headers=get_headers(api_key),
            )
            response.raise_for_status()
            data = response.json()

        short_url = f"{url_base}/{data['slug']}"
        console.print(f"[bold]Link: {data['slug']}[/bold]")
        console.print(f"  Short URL: [bold green]{short_url}[/bold green]")
        console.print(f"  Destination: {data['destination_url']}")
        console.print(f"  Clicks: [magenta]{data['click_count']}[/magenta]")
        console.print(f"  Created: {data['created_at']}")
        if data.get("expires_at"):
            console.print(f"  Expires: {data['expires_at']}")
        else:
            console.print(f"  Expires: [dim]Never[/dim]")
        if data.get("max_clicks"):
            console.print(f"  Max clicks: {data['max_clicks']}")
        else:
            console.print(f"  Max clicks: [dim]Unlimited[/dim]")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Link '{slug}' not found.")
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except httpx.RequestError as e:
        console.print(f"[red]Error:[/red] Could not connect to {url_base}: {e}")
        raise typer.Exit(1)


@app.command()
def stats(
    slug: str = typer.Argument(..., help="Link slug"),
    days: int = typer.Option(30, "--days", help="Number of days to analyze"),
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API server URL"
    ),
) -> None:
    """Show analytics for a link."""
    api_key = get_api_key()
    url_base = get_api_url(api_url)

    try:
        with httpx.Client() as client:
            response = client.get(
                f"{url_base}/analytics/{slug}",
                params={"days": days, "tz": "UTC"},
                headers=get_headers(api_key),
            )
            response.raise_for_status()
            data = response.json()

        console.print(f"[bold]Analytics for {data['slug']} (last {days} days)[/bold]\n")

        # Summary stats
        console.print(f"Total clicks: [magenta]{data['total_clicks']}[/magenta]")
        console.print(f"Unique IPs: [cyan]{data['unique_ips']}[/cyan]\n")

        # Clicks by day
        if data["clicks_by_day"]:
            console.print("[bold]Clicks by day:[/bold]")
            table = Table()
            table.add_column("Date", style="green")
            table.add_column("Clicks", justify="right", style="magenta")
            for day in data["clicks_by_day"]:
                table.add_row(day["date"], str(day["clicks"]))
            console.print(table)
            console.print()

        # Top referers
        if data["top_referers"]:
            console.print("[bold]Top referers:[/bold]")
            table = Table()
            table.add_column("Referer", style="blue")
            table.add_column("Clicks", justify="right", style="magenta")
            for ref in data["top_referers"][:10]:
                referer_display = ref["referer"] if ref["referer"] else "(direct)"
                table.add_row(referer_display, str(ref["clicks"]))
            console.print(table)
            console.print()

        # Top user agents
        if data["top_user_agents"]:
            console.print("[bold]Top user agents:[/bold]")
            table = Table()
            table.add_column("User Agent", style="blue")
            table.add_column("Clicks", justify="right", style="magenta")
            for ua in data["top_user_agents"][:10]:
                ua_display = ua["user_agent"][:60] if ua["user_agent"] else "(unknown)"
                table.add_row(ua_display, str(ua["clicks"]))
            console.print(table)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Link '{slug}' not found.")
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except httpx.RequestError as e:
        console.print(f"[red]Error:[/red] Could not connect to {url_base}: {e}")
        raise typer.Exit(1)


@app.command()
def delete(
    slug: str = typer.Argument(..., help="Link slug"),
    force: bool = typer.Option(
        False, "--force", "-f", help="Skip confirmation"
    ),
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API server URL"
    ),
) -> None:
    """Delete a short link."""
    api_key = get_api_key()
    url_base = get_api_url(api_url)

    if not force:
        if not typer.confirm(f"Delete link '{slug}'?"):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    try:
        with httpx.Client() as client:
            response = client.delete(
                f"{url_base}/links/{slug}",
                headers=get_headers(api_key),
            )
            response.raise_for_status()

        console.print(f"[green]✓[/green] Link '{slug}' deleted.")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Link '{slug}' not found.")
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except httpx.RequestError as e:
        console.print(f"[red]Error:[/red] Could not connect to {url_base}: {e}")
        raise typer.Exit(1)
