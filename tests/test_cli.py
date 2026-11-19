"""Tests for the Typer CLI client."""
from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner


cli = importlib.import_module("linkvault.cli.app")

runner = CliRunner()

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture()
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LINKVAULT_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("LINKVAULT_API_URL", raising=False)
    monkeypatch.setattr(cli, "console", Console(width=200))
    return tmp_path


def use_handler(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Route CLI HTTP traffic to *handler*; return the list of captured requests."""
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def make_client(api_url: str) -> httpx.Client:
        return httpx.Client(base_url=api_url.rstrip("/"), transport=httpx.MockTransport(wrapped))

    monkeypatch.setattr(cli, "_make_client", make_client)
    return seen


def store_key(config_dir: Path, key: str = "secret-key") -> None:
    (config_dir / "config.json").write_text(json.dumps({"api_key": key}))


def link_json(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "1",
        "user_id": "u",
        "slug": "abc123",
        "destination_url": "https://example.com/path",
        "expires_at": None,
        "max_clicks": None,
        "click_count": 3,
        "created_at": "2025-01-01T00:00:00",
        "deleted_at": None,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "command", [[], ["login"], ["logout"], ["shorten"], ["list"], ["info"], ["stats"], ["delete"]]
)
def test_help_works_everywhere(command: list[str]) -> None:
    result = runner.invoke(cli.app, [*command, "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_login_stores_api_key_and_logout_removes_it(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = use_handler(monkeypatch, lambda r: httpx.Response(200, json={"api_key": "k-123"}))
    result = runner.invoke(cli.app, ["login"], input="a@example.com\npw-123456\n")
    assert result.exit_code == 0, result.output
    assert json.loads(seen[0].content) == {"email": "a@example.com", "password": "pw-123456"}
    assert json.loads((config_dir / "config.json").read_text())["api_key"] == "k-123"

    result = runner.invoke(cli.app, ["logout"])
    assert result.exit_code == 0
    assert not (config_dir / "config.json").exists()


def test_login_failure_prints_error_in_red(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "console", Console(width=200, force_terminal=True, color_system="standard"))
    use_handler(
        monkeypatch,
        lambda r: httpx.Response(401, json={"error": "invalid_credentials", "detail": "Bad password."}),
    )
    result = runner.invoke(cli.app, ["login", "--email", "a@example.com", "--password", "x"])
    assert result.exit_code == 1
    assert "\x1b[31m" in result.output
    assert "Bad password." in result.output
    assert not (config_dir / "config.json").exists()


def test_commands_require_login(config_dir: Path) -> None:
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1
    assert "linkvault login" in result.output


def test_shorten_prints_bold_green_short_url(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "console", Console(width=200, force_terminal=True, color_system="standard"))
    store_key(config_dir)
    seen = use_handler(monkeypatch, lambda r: httpx.Response(201, json=link_json(slug="my-slug")))
    result = runner.invoke(
        cli.app,
        ["shorten", "https://example.com/x", "--slug", "my-slug", "--expires", "2030-01-31",
         "--max-clicks", "5", "--api-url", "http://short.test"],
    )
    assert result.exit_code == 0, result.output
    assert "\x1b[1;32mhttp://short.test/my-slug" in result.output
    request = seen[0]
    assert request.headers["Authorization"] == "Bearer secret-key"
    assert json.loads(request.content) == {
        "url": "https://example.com/x",
        "slug": "my-slug",
        "expires_at": "2030-01-31T23:59:59Z",
        "max_clicks": 5,
    }


def test_shorten_rejects_bad_expires(config_dir: Path) -> None:
    store_key(config_dir)
    result = runner.invoke(cli.app, ["shorten", "https://example.com", "--expires", "31/01/2030"])
    assert result.exit_code == 1
    assert "YYYY-MM-DD" in result.output


def test_api_url_from_environment(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store_key(config_dir)
    monkeypatch.setenv("LINKVAULT_API_URL", "http://env.test")
    use_handler(monkeypatch, lambda r: httpx.Response(201, json=link_json(slug="zz")))
    result = runner.invoke(cli.app, ["shorten", "https://example.com"])
    assert "http://env.test/zz" in result.output


def test_list_renders_table(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store_key(config_dir)
    long_url = "https://example.com/" + "a" * 100
    payload = {
        "total": 3,
        "page": 2,
        "page_size": 20,
        "items": [
            link_json(slug="live", destination_url=long_url),
            link_json(slug="old", expires_at="2020-01-01T00:00:00"),
            link_json(slug="gone", deleted_at="2024-01-01T00:00:00"),
        ],
    }
    seen = use_handler(monkeypatch, lambda r: httpx.Response(200, json=payload))
    result = runner.invoke(cli.app, ["list", "--page", "2"])
    assert result.exit_code == 0, result.output
    assert seen[0].url.params["page"] == "2"
    for header in ("Slug", "Destination", "Clicks", "Expires", "Status"):
        assert header in result.output
    assert long_url not in result.output
    assert long_url[:49] + "…" in result.output
    assert "active" in result.output
    assert "expired" in result.output
    assert "deleted" in result.output
    assert "never" in result.output


def test_info_shows_link(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store_key(config_dir)
    use_handler(monkeypatch, lambda r: httpx.Response(200, json=link_json(max_clicks=10)))
    result = runner.invoke(cli.app, ["info", "abc123"])
    assert result.exit_code == 0, result.output
    assert "https://example.com/path" in result.output
    assert "10" in result.output


def test_stats_renders_bar_chart(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store_key(config_dir)
    payload = {
        "slug": "abc123",
        "total_clicks": 12,
        "unique_ips": 4,
        "clicks_by_day": [
            {"date": "2025-01-14", "clicks": 4},
            {"date": "2025-01-15", "clicks": 8},
        ],
        "top_referers": [{"referer": "https://twitter.com", "clicks": 8}],
        "top_user_agents": [],
    }
    seen = use_handler(monkeypatch, lambda r: httpx.Response(200, json=payload))
    result = runner.invoke(cli.app, ["stats", "abc123", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert seen[0].url.params["days"] == "7"
    assert "2025-01-14" in result.output
    assert "2025-01-15" in result.output
    assert "12 clicks" in result.output
    assert "https://twitter.com" in result.output
    assert "━" in result.output  # progress bar glyphs


def test_delete_and_api_error(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store_key(config_dir)
    use_handler(monkeypatch, lambda r: httpx.Response(204))
    result = runner.invoke(cli.app, ["delete", "abc123"])
    assert result.exit_code == 0
    assert "Deleted" in result.output

    use_handler(monkeypatch, lambda r: httpx.Response(404, json={"detail": "Link not found."}))
    result = runner.invoke(cli.app, ["delete", "nope"])
    assert result.exit_code == 1
    assert "Link not found." in result.output


def test_connection_error_is_reported(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store_key(config_dir)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    use_handler(monkeypatch, boom)
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1
    assert "Could not reach" in result.output
