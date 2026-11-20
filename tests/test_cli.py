"""Tests for the Typer CLI client (spec item 7).

The CLI is exercised end-to-end through ``typer.testing.CliRunner``. Instead of
a real server, an in-memory ``FakeServer`` is plugged in via
``httpx.MockTransport``; it mimics the LinkVault REST API, including both of
its error shapes (``{"detail": ...}`` and ``{"error": ..., "detail": ...}``).
"""
from __future__ import annotations

import importlib
import io
import json
import re
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

# ``linkvault.cli`` re-exports the Typer object as ``app``, which shadows the
# submodule attribute, so load the module explicitly.
cli = importlib.import_module("linkvault.cli.app")

runner = CliRunner()

EMAIL = "alice@example.com"
PASSWORD = "s3cr3t!"
RESERVED = {"api", "admin", "health", "docs", "metrics"}


# ---------------------------------------------------------------------------
# Fake LinkVault server
# ---------------------------------------------------------------------------


class FakeServer:
    """A minimal in-memory imitation of the LinkVault REST API."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {
            EMAIL: {"id": "user-alice", "password": PASSWORD, "api_key": None},
            "bob@example.com": {"id": "user-bob", "password": "hunter22", "api_key": None},
        }
        self.links: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.analytics: dict[str, dict[str, Any]] = {}

    # -- helpers ----------------------------------------------------------

    def issue_key(self, email: str) -> str:
        key = str(uuid.uuid4())
        self.users[email]["api_key"] = key
        return key

    def add_link(self, owner_email: str, slug: str, url: str, **extra: Any) -> dict[str, Any]:
        link = {
            "id": str(uuid.uuid4()),
            "user_id": self.users[owner_email]["id"],
            "slug": slug,
            "destination_url": url,
            "expires_at": extra.get("expires_at"),
            "max_clicks": extra.get("max_clicks"),
            "click_count": extra.get("click_count", 0),
            "created_at": extra.get("created_at", "2025-01-01T12:00:00"),
            "deleted_at": None,
        }
        self.links[slug] = link
        return link

    def _user_for(self, request: httpx.Request) -> dict[str, Any] | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        key = auth[len("Bearer "):]
        for user in self.users.values():
            if user["api_key"] == key:
                return user
        return None

    def _owned_link(
        self, slug: str, user: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, httpx.Response | None]:
        link = self.links.get(slug.lower())
        if link is None or link["deleted_at"] is not None:
            return None, httpx.Response(404, json={"detail": "Link not found."})
        if link["user_id"] != user["id"]:
            return None, httpx.Response(403, json={"detail": "You do not own this link."})
        return link, None

    # -- request dispatch -------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method

        if method == "POST" and path == "/users/token":
            body = json.loads(request.content)
            user = self.users.get(body.get("email"))
            if user is None or user["password"] != body.get("password"):
                return httpx.Response(401, json={"detail": "Invalid email or password."})
            return httpx.Response(200, json={"api_key": self.issue_key(body["email"])})

        user = self._user_for(request)
        if user is None:
            return httpx.Response(401, json={"detail": "Invalid or missing API key."})

        if path == "/links" and method == "POST":
            body = json.loads(request.content)
            url = body.get("url", "")
            if not url.startswith(("http://", "https://")):
                return httpx.Response(
                    422,
                    json={
                        "error": "validation_error",
                        "detail": "body.url: Value error, url must start with http:// or https://",
                    },
                )
            slug = body.get("slug")
            if slug is not None:
                if slug.lower() in RESERVED:
                    return httpx.Response(
                        422,
                        json={"detail": f"'{slug}' is a reserved word and cannot be used as a slug."},
                    )
                slug = slug.lower()
                existing = self.links.get(slug)
                if existing is not None and existing["deleted_at"] is None:
                    return httpx.Response(409, json={"detail": f"Slug '{slug}' is already in use."})
            else:
                slug = "abc123"
            owner = next(e for e, u in self.users.items() if u is user)
            link = self.add_link(
                owner,
                slug,
                url,
                expires_at=body.get("expires_at"),
                max_clicks=body.get("max_clicks"),
            )
            return httpx.Response(201, json=link)

        if path == "/links" and method == "GET":
            page = int(request.url.params.get("page", "1"))
            page_size = 20
            mine = [
                lnk
                for lnk in self.links.values()
                if lnk["user_id"] == user["id"] and lnk["deleted_at"] is None
            ]
            start = (page - 1) * page_size
            return httpx.Response(
                200,
                json={
                    "total": len(mine),
                    "page": page,
                    "page_size": page_size,
                    "items": mine[start : start + page_size],
                },
            )

        match = re.fullmatch(r"/links/([^/]+)", path)
        if match:
            link, error = self._owned_link(match.group(1), user)
            if error is not None:
                return error
            assert link is not None
            if method == "GET":
                return httpx.Response(200, json=link)
            if method == "DELETE":
                link["deleted_at"] = "2025-01-02T00:00:00"
                return httpx.Response(204)

        match = re.fullmatch(r"/analytics/([^/]+)", path)
        if match and method == "GET":
            link, error = self._owned_link(match.group(1), user)
            if error is not None:
                return error
            assert link is not None
            days = int(request.url.params.get("days", "30"))
            data = self.analytics.get(link["slug"]) or {
                "slug": link["slug"],
                "total_clicks": 0,
                "unique_ips": 0,
                "clicks_by_day": [],
                "top_referers": [],
                "top_user_agents": [],
            }
            data = dict(data)
            data["clicks_by_day"] = data["clicks_by_day"][-days:]
            return httpx.Response(200, json=data)

        return httpx.Response(404, json={"error": "not_found"})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("LINKVAULT_API_URL", raising=False)
    # Wide terminal so Rich doesn't squeeze table columns.
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path


@pytest.fixture()
def server(home: Path, monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer()
    monkeypatch.setattr(cli, "_transport", httpx.MockTransport(fake.handle))
    return fake


@pytest.fixture()
def logged_in(server: FakeServer, home: Path) -> str:
    """Store a valid API key for Alice and return it."""
    key = server.issue_key(EMAIL)
    config = home / ".linkvault" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"api_key": key}))
    return key


def invoke(*args: str, input: str | None = None) -> Any:
    return runner.invoke(cli.app, list(args), input=input)


def config_file(home: Path) -> Path:
    return home / ".linkvault" / "config.json"


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


COMMANDS = ["login", "logout", "shorten", "list", "info", "stats", "delete"]


def test_root_help_lists_all_commands() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.output


def test_no_args_shows_help() -> None:
    result = invoke()
    assert result.exit_code == 0
    assert "shorten" in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_has_help(command: str) -> None:
    result = invoke(command, "--help")
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


# ---------------------------------------------------------------------------
# login / logout
# ---------------------------------------------------------------------------


def test_login_prompts_and_saves_key(server: FakeServer, home: Path) -> None:
    result = invoke("login", input=f"{EMAIL}\n{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    assert "Logged in" in result.output
    # The password is typed with hidden input, so it must not be echoed.
    assert PASSWORD not in result.output

    path = config_file(home)
    assert json.loads(path.read_text()) == {"api_key": server.users[EMAIL]["api_key"]}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_login_overwrites_previous_key_and_keeps_permissions(
    server: FakeServer, home: Path
) -> None:
    path = config_file(home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"api_key": "old"}))
    path.chmod(0o644)

    result = invoke("login", "--email", EMAIL, "--password", PASSWORD)
    assert result.exit_code == 0, result.output
    assert json.loads(path.read_text())["api_key"] == server.users[EMAIL]["api_key"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_login_wrong_password(server: FakeServer, home: Path) -> None:
    result = invoke("login", "--email", EMAIL, "--password", "nope")
    assert result.exit_code == 1
    assert "Invalid email or password." in result.output
    assert "Traceback" not in result.output
    assert not config_file(home).exists()


def test_logout_removes_credentials(logged_in: str, home: Path) -> None:
    result = invoke("logout")
    assert result.exit_code == 0
    assert "Logged out" in result.output
    assert not config_file(home).exists()


def test_logout_when_not_logged_in(home: Path) -> None:
    result = invoke("logout")
    assert result.exit_code == 0
    assert "not logged in" in result.output


def test_logout_then_list_asks_to_login(logged_in: str) -> None:
    assert invoke("logout").exit_code == 0
    result = invoke("list")
    assert result.exit_code == 1
    assert "Run `linkvault login` first" in result.output


def test_corrupt_config_asks_to_login(server: FakeServer, home: Path) -> None:
    path = config_file(home)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    result = invoke("list")
    assert result.exit_code == 1
    assert "linkvault login" in result.output


# ---------------------------------------------------------------------------
# shorten
# ---------------------------------------------------------------------------


def test_shorten_prints_full_short_url(server: FakeServer, logged_in: str) -> None:
    result = invoke("shorten", "https://example.com", "--slug", "demo")
    assert result.exit_code == 0, result.output
    assert "http://localhost:8000/demo" in result.output

    sent = json.loads(server.requests[-1].content)
    assert sent == {"url": "https://example.com", "slug": "demo"}
    assert server.requests[-1].headers["Authorization"] == f"Bearer {logged_in}"


def test_shorten_output_is_bold_green(
    server: FakeServer, logged_in: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    buffer = io.StringIO()
    monkeypatch.setattr(
        cli, "console", Console(file=buffer, force_terminal=True, color_system="standard")
    )
    result = invoke("shorten", "https://example.com")
    assert result.exit_code == 0, result.output
    # ANSI SGR 1 = bold, 32 = green
    assert "\x1b[1;32mhttp://localhost:8000/abc123" in buffer.getvalue()


def test_shorten_sends_expiry_and_max_clicks(server: FakeServer, logged_in: str) -> None:
    result = invoke(
        "shorten", "https://example.com", "--expires", "2030-01-02", "--max-clicks", "5"
    )
    assert result.exit_code == 0, result.output
    sent = json.loads(server.requests[-1].content)
    assert sent["expires_at"] == "2030-01-02T23:59:59Z"
    assert sent["max_clicks"] == 5


def test_shorten_bad_date_does_not_call_server(server: FakeServer, logged_in: str) -> None:
    result = invoke("shorten", "https://example.com", "--expires", "02/01/2030")
    assert result.exit_code == 1
    assert "YYYY-MM-DD" in result.output
    assert server.requests == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--slug", "admin"], "reserved word"),
        (["--slug", "taken"], "already in use"),
    ],
)
def test_shorten_slug_errors(
    server: FakeServer, logged_in: str, args: list[str], message: str
) -> None:
    server.add_link(EMAIL, "taken", "https://example.org")
    result = invoke("shorten", "https://example.com", *args)
    assert result.exit_code == 1
    assert message in result.output


def test_shorten_invalid_url_uses_error_envelope_detail(
    server: FakeServer, logged_in: str
) -> None:
    result = invoke("shorten", "ftp://example.com")
    assert result.exit_code == 1
    assert "url must start with http:// or https://" in result.output


def test_shorten_requires_login(server: FakeServer) -> None:
    result = invoke("shorten", "https://example.com")
    assert result.exit_code == 1
    assert "Run `linkvault login` first" in result.output
    assert server.requests == []


def test_invalid_key_suggests_login_again(server: FakeServer, home: Path) -> None:
    path = config_file(home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"api_key": "stale-key"}))
    result = invoke("list")
    assert result.exit_code == 1
    assert "Invalid or missing API key." in result.output
    assert "linkvault login" in result.output


# ---------------------------------------------------------------------------
# API URL resolution
# ---------------------------------------------------------------------------


def test_api_url_defaults_to_localhost(server: FakeServer, logged_in: str) -> None:
    assert invoke("list").exit_code == 0
    assert str(server.requests[-1].url).startswith("http://localhost:8000/links")


def test_env_var_overrides_default(
    server: FakeServer, logged_in: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINKVAULT_API_URL", "http://env.example:9000")
    result = invoke("shorten", "https://example.com", "--slug", "envy")
    assert result.exit_code == 0, result.output
    assert "http://env.example:9000/envy" in result.output
    assert server.requests[-1].url.host == "env.example"


def test_flag_overrides_env_var(
    server: FakeServer, logged_in: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINKVAULT_API_URL", "http://env.example:9000")
    result = invoke(
        "shorten", "https://example.com", "--slug", "flag", "--api-url", "http://flag.example/"
    )
    assert result.exit_code == 0, result.output
    assert "http://flag.example/flag" in result.output
    assert server.requests[-1].url.host == "flag.example"


def test_server_not_running(logged_in: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    monkeypatch.setattr(cli, "_transport", httpx.MockTransport(refuse))
    result = invoke("list", "--api-url", "http://127.0.0.1:9")
    assert result.exit_code == 1
    assert "Could not connect to http://127.0.0.1:9. Is the server running?" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_renders_table(server: FakeServer, logged_in: str) -> None:
    long_url = "https://example.com/" + "x" * 100
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    server.add_link(EMAIL, "active", long_url, click_count=3)
    server.add_link(EMAIL, "old", "https://old.example", expires_at=past)
    server.add_link(EMAIL, "capped", "https://capped.example", max_clicks=2, click_count=2)
    server.add_link("bob@example.com", "bobs", "https://bob.example")

    result = invoke("list")
    assert result.exit_code == 0, result.output
    for header in ("Slug", "Destination", "Clicks", "Expires", "Status"):
        assert header in result.output
    assert "Active" in result.output
    assert "Expired" in result.output
    assert "Exhausted" in result.output
    assert "bobs" not in result.output
    assert cli.truncate(long_url) in result.output
    assert long_url not in result.output
    assert "3 links in total" in result.output


def test_list_passes_page(server: FakeServer, logged_in: str) -> None:
    result = invoke("list", "--page", "3")
    assert result.exit_code == 0, result.output
    assert server.requests[-1].url.params["page"] == "3"


def test_list_empty(server: FakeServer, logged_in: str) -> None:
    result = invoke("list")
    assert result.exit_code == 0
    assert "no links yet" in result.output


def test_truncate_limits_to_50_chars() -> None:
    assert cli.truncate("a" * 50) == "a" * 50
    cut = cli.truncate("a" * 80)
    assert len(cut) == 50
    assert cut.endswith("…")


def test_link_status() -> None:
    now = datetime(2025, 6, 1, tzinfo=timezone.utc)
    assert cli.link_status({"expires_at": None, "max_clicks": None}, now) == "Active"
    assert cli.link_status({"expires_at": "2025-05-31T00:00:00"}, now) == "Expired"
    assert cli.link_status({"expires_at": "2025-06-02T00:00:00Z"}, now) == "Active"
    assert cli.link_status({"max_clicks": 5, "click_count": 5}, now) == "Exhausted"
    assert cli.link_status({"max_clicks": 5, "click_count": 4}, now) == "Active"


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def test_info_shows_details(server: FakeServer, logged_in: str) -> None:
    server.add_link(
        EMAIL,
        "demo",
        "https://example.com/page",
        click_count=7,
        max_clicks=100,
        expires_at="2030-01-02T23:59:59",
    )
    result = invoke("info", "demo")
    assert result.exit_code == 0, result.output
    assert "https://example.com/page" in result.output
    assert "http://localhost:8000/demo" in result.output
    assert "7" in result.output
    assert "100" in result.output
    assert "2030-01-02 23:59 UTC" in result.output
    assert "Active" in result.output


def test_info_unknown_slug(server: FakeServer, logged_in: str) -> None:
    result = invoke("info", "missing")
    assert result.exit_code == 1
    assert "Link not found." in result.output


def test_info_someone_elses_link(server: FakeServer, logged_in: str) -> None:
    server.add_link("bob@example.com", "bobs", "https://bob.example")
    result = invoke("info", "bobs")
    assert result.exit_code == 1
    assert "You do not own this link." in result.output


def test_error_with_only_error_code(server: FakeServer, logged_in: str) -> None:
    # Unknown routes in the fake return {"error": "not_found"} only.
    response = httpx.Response(404, json={"error": "not_found"})
    assert cli._error_message(response) == "not_found"


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_renders_chart(server: FakeServer, logged_in: str) -> None:
    server.add_link(EMAIL, "demo", "https://example.com")
    server.analytics["demo"] = {
        "slug": "demo",
        "total_clicks": 12,
        "unique_ips": 9,
        "clicks_by_day": [
            {"date": "2025-01-14", "clicks": 0},
            {"date": "2025-01-15", "clicks": 4},
            {"date": "2025-01-16", "clicks": 8},
        ],
        "top_referers": [{"referer": "https://twitter.com", "clicks": 6}],
        "top_user_agents": [{"user_agent": "Mozilla/5.0 Test", "clicks": 12}],
    }
    result = invoke("stats", "demo", "--days", "3")
    assert result.exit_code == 0, result.output
    assert server.requests[-1].url.params["days"] == "3"
    assert "12" in result.output
    assert "9" in result.output
    for day in ("2025-01-14", "2025-01-15", "2025-01-16"):
        assert day in result.output
    # The busiest day's bar is a full-width run of bar characters.
    assert "━" * cli.BAR_WIDTH in result.output
    assert "https://twitter.com" in result.output
    assert "Mozilla/5.0 Test" in result.output


def test_stats_default_days(server: FakeServer, logged_in: str) -> None:
    server.add_link(EMAIL, "demo", "https://example.com")
    result = invoke("stats", "demo")
    assert result.exit_code == 0, result.output
    assert server.requests[-1].url.params["days"] == "30"
    assert "No click data." in result.output


@pytest.mark.parametrize("days", ["0", "366"])
def test_stats_days_out_of_range(server: FakeServer, logged_in: str, days: str) -> None:
    result = invoke("stats", "demo", "--days", days)
    assert result.exit_code != 0
    assert server.requests == []


def test_stats_someone_elses_link(server: FakeServer, logged_in: str) -> None:
    server.add_link("bob@example.com", "bobs", "https://bob.example")
    result = invoke("stats", "bobs")
    assert result.exit_code == 1
    assert "You do not own this link." in result.output


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_link(server: FakeServer, logged_in: str) -> None:
    server.add_link(EMAIL, "demo", "https://example.com")
    result = invoke("delete", "demo")
    assert result.exit_code == 0, result.output
    assert "Deleted link 'demo'" in result.output
    assert server.requests[-1].method == "DELETE"
    assert server.links["demo"]["deleted_at"] is not None


def test_delete_unknown_slug(server: FakeServer, logged_in: str) -> None:
    result = invoke("delete", "missing")
    assert result.exit_code == 1
    assert "Link not found." in result.output
