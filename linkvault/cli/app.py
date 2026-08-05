"""Typer CLI for LinkVault."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table

app = typer.Typer(name="linkvault", help="LinkVault CLI client.")
console = Console()

# Configuration file path
CONFIG_DIR = Path.home() / ".linkvault"
CONFIG_FILE = CONFIG_DIR / "config.json"


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def get_config() -> dict[str, Any]:
    """Load configuration from ~/.linkvault/config.json."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(config: dict[str, Any]) -> None:
    """Save configuration to ~/.linkvault/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_api_key() -> str | None:
    """Get the stored API key."""
    config = get_config()
    api_key = config.get("api_key")
    return str(api_key) if api_key is not None else None


def get_base_url(api_url: str | None) -> str:
    """Get the base URL from flag, env var, or config."""
    if api_url:
        return api_url
    env_url = os.environ.get("LINKVAULT_API_URL")
    if env_url:
        return env_url
    config = get_config()
    base = config.get("api_url", "http://localhost:8000")
    return str(base) if base is not None else "http://localhost:8000"


def handle_error(response: httpx.Response) -> None:
    """Print error message from API response and exit."""
    try:
        error_data = response.json()
        detail = error_data.get("detail", "Unknown error")
        console.print(f"[bold red]Error:[/bold red] {detail}")
    except Exception:
        console.print(f"[bold red]Error:[/bold red] HTTP {response.status_code}")
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def login(
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API base URL (default: http://localhost:8000)"
    ),
) -> None:
    """Authenticate and store API key locally."""
    base_url = get_base_url(api_url)
    
    email = typer.prompt("Email")
    password = typer.prompt("Password", hide_input=True)
    
    try:
        response = httpx.post(
            f"{base_url}/users/token",
            json={"email": email, "password": password},
            timeout=10.0,
        )
        
        if response.status_code != 200:
            handle_error(response)
        
        data = response.json()
        api_key = data["api_key"]
        
        save_config({"api_key": api_key, "api_url": base_url})
        console.print("[bold green]✓[/bold green] Logged in successfully!")
        
    except httpx.RequestError as e:
        console.print(f"[bold red]Error:[/bold red] Could not connect to {base_url}")
        console.print(f"  {e}")
        raise typer.Exit(code=1)


@app.command()
def logout() -> None:
    """Remove stored credentials."""
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
        console.print("[bold green]✓[/bold green] Logged out successfully!")
    else:
        console.print("[yellow]No stored credentials found.[/yellow]")


@app.command()
def shorten(
    url: str,
    slug: Optional[str] = typer.Option(None, "--slug", help="Custom slug"),
    expires: Optional[str] = typer.Option(
        None, "--expires", help="Expiry date (YYYY-MM-DD)"
    ),
    max_clicks: Optional[int] = typer.Option(
        None, "--max-clicks", help="Maximum number of clicks"
    ),
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API base URL"
    ),
) -> None:
    """Create a new short link."""
    api_key = get_api_key()
    if not api_key:
        console.print("[bold red]Error:[/bold red] Not logged in. Run 'linkvault login' first.")
        raise typer.Exit(code=1)
    
    base_url = get_base_url(api_url)
    
    # Build request payload
    payload: dict[str, Any] = {"url": url}
    if slug:
        payload["slug"] = slug
    if expires:
        # Convert YYYY-MM-DD to ISO datetime
        try:
            dt = datetime.strptime(expires, "%Y-%m-%d")
            payload["expires_at"] = dt.isoformat() + "Z"
        except ValueError:
            console.print("[bold red]Error:[/bold red] Invalid date format. Use YYYY-MM-DD.")
            raise typer.Exit(code=1)
    if max_clicks is not None:
        payload["max_clicks"] = max_clicks
    
    try:
        response = httpx.post(
            f"{base_url}/links",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        
        if response.status_code != 201:
            handle_error(response)
        
        data = response.json()
        short_url = f"{base_url}/{data['slug']}"
        console.print(f"[bold green]{short_url}[/bold green]")
        
    except httpx.RequestError as e:
        console.print(f"[bold red]Error:[/bold red] Could not connect to {base_url}")
        console.print(f"  {e}")
        raise typer.Exit(code=1)


@app.command()
def list(
    page: int = typer.Option(1, "--page", help="Page number"),
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API base URL"
    ),
) -> None:
    """List your short links."""
    api_key = get_api_key()
    if not api_key:
        console.print("[bold red]Error:[/bold red] Not logged in. Run 'linkvault login' first.")
        raise typer.Exit(code=1)
    
    base_url = get_base_url(api_url)
    
    try:
        response = httpx.get(
            f"{base_url}/links",
            params={"page": page, "page_size": 20},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        
        if response.status_code != 200:
            handle_error(response)
        
        data = response.json()
        items = data["items"]
        total = data["total"]
        
        if not items:
            console.print("[yellow]No links found.[/yellow]")
            return
        
        # Create Rich table
        table = Table(title=f"Your Links (Page {page}, Total: {total})")
        table.add_column("Slug", style="cyan")
        table.add_column("Destination", style="white", max_width=50)
        table.add_column("Clicks", justify="right", style="green")
        table.add_column("Expires", style="yellow")
        table.add_column("Status", style="magenta")
        
        for item in items:
            slug = item["slug"]
            dest = item["destination_url"]
            if len(dest) > 50:
                dest = dest[:47] + "..."
            clicks = str(item["click_count"])
            
            # Format expiry
            expires_str = ""
            if item["expires_at"]:
                try:
                    exp_dt = datetime.fromisoformat(item["expires_at"].replace("Z", "+00:00"))
                    expires_str = exp_dt.strftime("%Y-%m-%d")
                except Exception:
                    expires_str = "Invalid"
            else:
                expires_str = "Never"
            
            # Determine status
            status = "Active"
            if item["deleted_at"]:
                status = "Deleted"
            elif item["expires_at"]:
                try:
                    exp_dt = datetime.fromisoformat(item["expires_at"].replace("Z", "+00:00"))
                    if exp_dt < datetime.now(exp_dt.tzinfo):
                        status = "Expired"
                except Exception:
                    pass
            if item["max_clicks"] and item["click_count"] >= item["max_clicks"]:
                status = "Exhausted"
            
            table.add_row(slug, dest, clicks, expires_str, status)
        
        console.print(table)
        
    except httpx.RequestError as e:
        console.print(f"[bold red]Error:[/bold red] Could not connect to {base_url}")
        console.print(f"  {e}")
        raise typer.Exit(code=1)


@app.command()
def info(
    slug: str,
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API base URL"
    ),
) -> None:
    """Show detailed information about a link."""
    api_key = get_api_key()
    if not api_key:
        console.print("[bold red]Error:[/bold red] Not logged in. Run 'linkvault login' first.")
        raise typer.Exit(code=1)
    
    base_url = get_base_url(api_url)
    
    try:
        response = httpx.get(
            f"{base_url}/links/{slug}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        
        if response.status_code != 200:
            handle_error(response)
        
        data = response.json()
        
        console.print(f"\n[bold cyan]Link Information[/bold cyan]")
        console.print(f"  Slug:        {data['slug']}")
        console.print(f"  URL:         {data['destination_url']}")
        console.print(f"  Clicks:      {data['click_count']}")
        console.print(f"  Max Clicks:  {data['max_clicks'] or 'Unlimited'}")
        
        if data['expires_at']:
            try:
                exp_dt = datetime.fromisoformat(data['expires_at'].replace("Z", "+00:00"))
                console.print(f"  Expires:     {exp_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            except Exception:
                console.print(f"  Expires:     {data['expires_at']}")
        else:
            console.print(f"  Expires:     Never")
        
        try:
            created_dt = datetime.fromisoformat(data['created_at'].replace("Z", "+00:00"))
            console.print(f"  Created:     {created_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        except Exception:
            console.print(f"  Created:     {data['created_at']}")
        
        console.print()
        
    except httpx.RequestError as e:
        console.print(f"[bold red]Error:[/bold red] Could not connect to {base_url}")
        console.print(f"  {e}")
        raise typer.Exit(code=1)


@app.command()
def stats(
    slug: str,
    days: int = typer.Option(30, "--days", help="Number of days to show"),
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API base URL"
    ),
) -> None:
    """Show analytics for a link with a bar chart."""
    api_key = get_api_key()
    if not api_key:
        console.print("[bold red]Error:[/bold red] Not logged in. Run 'linkvault login' first.")
        raise typer.Exit(code=1)
    
    base_url = get_base_url(api_url)
    
    try:
        response = httpx.get(
            f"{base_url}/analytics/{slug}",
            params={"days": days},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        
        if response.status_code != 200:
            handle_error(response)
        
        data = response.json()
        
        console.print(f"\n[bold cyan]Analytics for {data['slug']}[/bold cyan]")
        console.print(f"  Total Clicks:  {data['total_clicks']}")
        console.print(f"  Unique IPs:    {data['unique_ips']}")
        console.print()
        
        # Show clicks by day as a bar chart
        clicks_by_day = data.get("clicks_by_day", [])
        if clicks_by_day:
            console.print("[bold]Clicks by Day:[/bold]")
            max_clicks = max((day["clicks"] for day in clicks_by_day), default=1)
            
            for day in clicks_by_day:
                date_str = day["date"]
                clicks = day["clicks"]
                bar_width = int((clicks / max_clicks) * 40) if max_clicks > 0 else 0
                bar = "█" * bar_width
                console.print(f"  {date_str}  {bar} {clicks}")
            console.print()
        
        # Show top referers
        top_referers = data.get("top_referers", [])
        if top_referers:
            console.print("[bold]Top Referers:[/bold]")
            for ref in top_referers[:5]:
                referer = ref["referer"] or "(direct)"
                clicks = ref["clicks"]
                console.print(f"  {referer[:60]:<60} {clicks:>5}")
            console.print()
        
        # Show top user agents
        top_user_agents = data.get("top_user_agents", [])
        if top_user_agents:
            console.print("[bold]Top User Agents:[/bold]")
            for ua in top_user_agents[:5]:
                user_agent = ua["user_agent"] or "(unknown)"
                clicks = ua["clicks"]
                # Truncate long user agents
                if len(user_agent) > 60:
                    user_agent = user_agent[:57] + "..."
                console.print(f"  {user_agent:<60} {clicks:>5}")
            console.print()
        
    except httpx.RequestError as e:
        console.print(f"[bold red]Error:[/bold red] Could not connect to {base_url}")
        console.print(f"  {e}")
        raise typer.Exit(code=1)


@app.command()
def delete(
    slug: str,
    api_url: Optional[str] = typer.Option(
        None, "--api-url", help="API base URL"
    ),
) -> None:
    """Delete a short link."""
    api_key = get_api_key()
    if not api_key:
        console.print("[bold red]Error:[/bold red] Not logged in. Run 'linkvault login' first.")
        raise typer.Exit(code=1)
    
    base_url = get_base_url(api_url)
    
    # Confirm deletion
    confirm = typer.confirm(f"Are you sure you want to delete '{slug}'?")
    if not confirm:
        console.print("[yellow]Cancelled.[/yellow]")
        return
    
    try:
        response = httpx.delete(
            f"{base_url}/links/{slug}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        
        if response.status_code != 204:
            handle_error(response)
        
        console.print(f"[bold green]✓[/bold green] Link '{slug}' deleted successfully!")
        
    except httpx.RequestError as e:
        console.print(f"[bold red]Error:[/bold red] Could not connect to {base_url}")
        console.print(f"  {e}")
        raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """LinkVault CLI client."""
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


if __name__ == "__main__":
    app()
