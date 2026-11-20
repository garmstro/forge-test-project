"""
Tests for the LinkVault CLI (Item 7).

The CLI is run with Typer's CliRunner and its HTTP client is swapped for a
Starlette TestClient bound to the real FastAPI app, so every command talks to
the actual API in-process — no running server required.  Each test gets a
fresh SQLite database (a temporary file, because the TestClient runs the app
on its own event loop) and a temporary HOME directory for credentials.
"""
from __future__ import annotations

import importlib
import json
import os
import stat
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from linkvault.cli.app import app as cli_app
from linkvault.database import Base, get_db
from linkvault.main import app as api_app

# ``linkvault.cli.app`` the module is shadowed by the ``app`` Typer object
# re-exported from ``linkvault.cli``, so import the module explicitly.
cli_module = importlib.import_module("linkvault.cli.app")

API_URL = "http://testserver"
PASSWORD = "password123"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("LINKVAULT_API_URL", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    return home_dir


@pytest.fixture()
def server(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Wire the CLI to the in-process API backed by a fresh database."""
    db_file = tmp_path / "cli-test.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", poolclass=NullPool)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db() -> Any:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    api_app.dependency_overrides[get_db] = override_get_db

    def fake_build_client(api_url: str, api_key: str | None) -> httpx.Client:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return TestClient(api_app, base_url=api_url, headers=headers)

    monkeypatch.setattr(cli_module, "build_client", fake_build_client)

    with TestClient(api_app, base_url=API_URL) as raw:
        yield raw

    api_app.dependency_overrides.clear()


runner = CliRunner()


def run(*args: str, input: str | None = None, env: dict[str, str] | None = None) -> Any:
    return runner.invoke(cli_app, list(args), input=input, env=env)


def register(client: TestClient, email: str) -> str:
    resp = client.post("/users/register", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["api_key"])


def login(email: str) -> Any:
    result = run("login", "--email", email, "--password", PASSWORD, "--api-url", API_URL)
    assert result.exit_code == 0, result.output
    return result


def shorten(*args: str) -> Any:
    return run("shorten", *args, "--api-url", API_URL)


def stored_key(home: Path) -> str:
    data = json.loads((home / ".linkvault" / "config.json").read_text())
    return str(data["api_key"])


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command", [[], ["login"], ["logout"], ["shorten"], ["list"], ["info"], ["stats"], ["delete"]]
)
def test_help_works_on_every_command(home: Path, command: list[str]) -> None:
    result = run(*command, "--help")
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


def test_no_command_shows_help(home: Path) -> None:
    result = run()
    assert result.exit_code == 0
    assert "shorten" in result.output and "stats" in result.output


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


def test_login_prompts_and_stores_private_config(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    result = run("login", "--api-url", API_URL, input=f"alice@example.com\n{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    assert "Logged in as alice@example.com" in result.output
    assert "replaces the previous one" in result.output
    # Password is not echoed back
    assert PASSWORD not in result.output

    config_file = home / ".linkvault" / "config.json"
    assert config_file.exists()
    assert stat.S_IMODE(os.stat(config_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(config_file.parent).st_mode) == 0o700
    key = stored_key(home)
    assert server.get("/links", headers={"Authorization": f"Bearer {key}"}).status_code == 200


def test_login_wrong_password_is_red_error(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    result = run(
        "login", "--email", "alice@example.com", "--password", "wrong", "--api-url", API_URL,
        env={"FORCE_COLOR": "1"},
    )
    assert result.exit_code == 1
    assert "Invalid email or password." in result.stderr
    assert "\x1b[1;31m" in result.stderr  # bold red
    assert not (home / ".linkvault" / "config.json").exists()


def test_logout_removes_credentials(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    result = run("logout")
    assert result.exit_code == 0
    assert "Logged out" in result.output
    assert not (home / ".linkvault" / "config.json").exists()

    again = run("logout")
    assert again.exit_code == 0
    assert "not logged in" in again.output


@pytest.mark.parametrize(
    "command",
    [
        ["shorten", "https://example.com"],
        ["list"],
        ["info", "demo"],
        ["stats", "demo"],
        ["delete", "demo", "--yes"],
    ],
)
def test_protected_commands_require_login(server: TestClient, command: list[str]) -> None:
    result = run(*command, "--api-url", API_URL, env={"FORCE_COLOR": "1"})
    assert result.exit_code == 1
    assert "not logged in" in result.stderr
    assert "linkvault login" in result.stderr
    assert "\x1b[1;31m" in result.stderr


def test_stale_key_after_relogin_elsewhere(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    # Another device logs in, rotating the key.
    server.post("/users/token", json={"email": "alice@example.com", "password": PASSWORD})
    result = run("list", "--api-url", API_URL)
    assert result.exit_code == 1
    assert "Invalid or missing API key." in result.stderr
    assert "login" in result.stderr


# ---------------------------------------------------------------------------
# Shorten
# ---------------------------------------------------------------------------


def test_shorten_prints_bold_green_short_url(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    result = run(
        "shorten", "https://example.com", "--slug", "demo", "--api-url", API_URL,
        env={"FORCE_COLOR": "1"},
    )
    assert result.exit_code == 0, result.output
    assert f"{API_URL}/demo" in result.stdout
    assert "\x1b[1;32m" in result.stdout  # bold green


def test_shorten_auto_slug_with_options(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    result = shorten("https://example.com/x", "--expires", "2099-12-31", "--max-clicks", "5")
    assert result.exit_code == 0, result.output
    slug = result.stdout.strip().rsplit("/", 1)[-1]
    assert len(slug) == 6

    resp = server.get(f"/links/{slug}", headers={"Authorization": f"Bearer {stored_key(home)}"})
    body = resp.json()
    assert body["max_clicks"] == 5
    assert body["expires_at"].startswith("2099-12-31T23:59:59")


def test_shorten_uses_env_api_url(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    result = run(
        "shorten", "https://example.com", "--slug", "envslug",
        env={"LINKVAULT_API_URL": "http://short.example/"},
    )
    assert result.exit_code == 0, result.output
    assert "http://short.example/envslug" in result.stdout


def test_shorten_bad_date_rejected_before_request(
    server: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")

    def boom(api_url: str, api_key: str | None) -> httpx.Client:
        raise AssertionError("no request should be sent")

    monkeypatch.setattr(cli_module, "build_client", boom)
    result = shorten("https://example.com", "--expires", "31/12/2026")
    assert result.exit_code == 1
    assert "YYYY-MM-DD" in result.stderr


def test_shorten_taken_slug_is_red_error(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    assert shorten("https://example.com", "--slug", "demo").exit_code == 0
    result = shorten("https://example.org", "--slug", "demo")
    assert result.exit_code == 1
    assert "Slug 'demo' is already in use." in result.stderr


def test_shorten_invalid_url_shows_validation_message(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    result = shorten("ftp://example.com")
    assert result.exit_code == 1
    assert "http:// or https://" in result.stderr


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_empty_message(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    result = run("list", "--api-url", API_URL)
    assert result.exit_code == 0
    assert "no links yet" in result.output


def test_list_table_truncation_and_statuses(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    headers = {"Authorization": f"Bearer {stored_key(home)}"}
    long_url = "https://example.com/" + "a" * 100
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    assert server.post("/links", json={"url": long_url, "slug": "demo"}, headers=headers).status_code == 201
    assert server.post(
        "/links", json={"url": "https://example.com/old", "slug": "old", "expires_at": past},
        headers=headers,
    ).status_code == 201
    assert server.post(
        "/links", json={"url": "https://example.com/once", "slug": "once", "max_clicks": 1},
        headers=headers,
    ).status_code == 201
    assert server.get("/once", follow_redirects=False).status_code == 302

    result = run("list", "--api-url", API_URL)
    assert result.exit_code == 0, result.output
    out = result.output
    for column in ("Slug", "Destination", "Clicks", "Expires", "Status"):
        assert column in out
    truncated = long_url[:47] + "..."
    assert len(truncated) == 50
    assert truncated in out
    assert long_url[:48] not in out

    rows = {line.split("│")[1].strip(): line for line in out.splitlines() if line.count("│") >= 5}
    assert "active" in rows["demo"]
    assert "expired" in rows["old"]
    assert "used up" in rows["once"]
    assert "Page 1 of 1" in out and "3 link(s) total" in out


def test_list_pagination(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    headers = {"Authorization": f"Bearer {stored_key(home)}"}
    for i in range(cli_module.LIST_PAGE_SIZE + 1):
        server.post("/links", json={"url": f"https://example.com/{i}"}, headers=headers)
    result = run("list", "--page", "2", "--api-url", API_URL)
    assert result.exit_code == 0
    assert "Page 2 of 2" in result.output
    assert "21 link(s) total" in result.output


# ---------------------------------------------------------------------------
# Info / stats
# ---------------------------------------------------------------------------


def test_info_and_stats_after_visits(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    assert shorten("https://example.com", "--slug", "demo").exit_code == 0
    for referer in ("https://twitter.com", "https://twitter.com", "https://news.ycombinator.com"):
        resp = server.get(
            "/demo", follow_redirects=False,
            headers={"referer": referer, "user-agent": "TestBrowser/1.0"},
        )
        assert resp.status_code == 301

    info = run("info", "demo", "--api-url", API_URL)
    assert info.exit_code == 0, info.output
    assert "https://example.com" in info.output
    assert f"{API_URL}/demo" in info.output
    clicks_line = next(line for line in info.output.splitlines() if "Clicks" in line and "Max" not in line)
    assert "3" in clicks_line
    assert "active" in info.output

    stats = run("stats", "demo", "--days", "7", "--api-url", API_URL)
    assert stats.exit_code == 0, stats.output
    out = stats.output
    assert "Total clicks: 3" in out
    assert "Unique visitors (IPs): 1" in out
    assert "Clicks by day" in out
    today = datetime.now(timezone.utc).date().isoformat()
    day_lines = [line for line in out.splitlines() if line.strip()[:4].isdigit() and "-" in line[:12]]
    assert len(day_lines) == 7
    today_line = next(line for line in day_lines if today in line)
    assert "━" in today_line and today_line.rstrip().endswith("3")
    assert "https://twitter.com" in out and "TestBrowser/1.0" in out
    assert out.index("https://twitter.com") < out.index("https://news.ycombinator.com")


def test_link_owned_by_someone_else(server: TestClient, home: Path) -> None:
    bob_key = register(server, "bob@example.com")
    server.post("/links", json={"url": "https://bob.example", "slug": "bobs"},
                headers={"Authorization": f"Bearer {bob_key}"})
    register(server, "alice@example.com")
    login("alice@example.com")
    for command in (["info", "bobs"], ["stats", "bobs"], ["delete", "bobs", "--yes"]):
        result = run(*command, "--api-url", API_URL)
        assert result.exit_code == 1, command
        assert "You do not own this link." in result.stderr


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_confirms_then_removes(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    assert shorten("https://example.com", "--slug", "demo").exit_code == 0

    declined = run("delete", "demo", "--api-url", API_URL, input="n\n")
    assert declined.exit_code == 0
    assert "nothing was deleted" in declined.output
    assert run("info", "demo", "--api-url", API_URL).exit_code == 0

    confirmed = run("delete", "demo", "--api-url", API_URL, input="y\n")
    assert confirmed.exit_code == 0, confirmed.output
    assert "Delete link 'demo'?" in confirmed.output
    assert "Deleted link 'demo'" in confirmed.output

    after = run("info", "demo", "--api-url", API_URL, env={"FORCE_COLOR": "1"})
    assert after.exit_code == 1
    assert "Link not found." in after.stderr
    assert "\x1b[1;31m" in after.stderr

    # Slug is reusable
    assert shorten("https://example.org", "--slug", "demo").exit_code == 0


def test_delete_yes_skips_prompt(server: TestClient, home: Path) -> None:
    register(server, "alice@example.com")
    login("alice@example.com")
    assert shorten("https://example.com", "--slug", "demo").exit_code == 0
    result = run("delete", "demo", "--yes", "--api-url", API_URL)
    assert result.exit_code == 0
    assert "Delete link" not in result.output


# ---------------------------------------------------------------------------
# Connectivity and error parsing
# ---------------------------------------------------------------------------


def test_server_unreachable(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def build(api_url: str, api_key: str | None) -> httpx.Client:
        return httpx.Client(base_url=api_url, transport=httpx.MockTransport(refuse))

    monkeypatch.setattr(cli_module, "build_client", build)
    result = run("login", "--email", "a@example.com", "--password", "x",
                 "--api-url", "http://127.0.0.1:9", env={"FORCE_COLOR": "1"})
    assert result.exit_code == 1
    assert "Could not connect to the LinkVault server at http://127.0.0.1:9" in result.stderr
    assert "\x1b[1;31m" in result.stderr


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (409, {"detail": "Slug 'x' is already in use."}, "Slug 'x' is already in use."),
        (422, {"error": "validation_error", "detail": "body.url: bad"}, "body.url: bad"),
        (404, {"error": "not_found"}, "Not found"),
        (422, {"detail": [{"msg": "field required"}, {"msg": "bad"}]}, "field required; bad"),
    ],
)
def test_api_error_message_formats(status: int, body: dict[str, Any], expected: str) -> None:
    response = httpx.Response(status, json=body)
    assert cli_module.api_error_message(response) == expected


def test_api_error_message_non_json() -> None:
    assert cli_module.api_error_message(httpx.Response(502, text="Bad gateway")) == "Bad gateway"
    assert cli_module.api_error_message(httpx.Response(500)) == "Internal Server Error"
