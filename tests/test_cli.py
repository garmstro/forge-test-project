"""Tests for the LinkVault CLI (linkvault.cli).

Most tests drive the Typer app with an ``httpx.MockTransport`` so they are
fast and deterministic. ``test_end_to_end_against_real_app`` runs the CLI
against the real FastAPI app through Starlette's TestClient.
"""
from __future__ import annotations

import importlib
import json
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from linkvault.cli import config
from linkvault.cli.client import LinkVaultClient

# `linkvault.cli.app` is shadowed by the Typer object re-exported from
# linkvault/cli/__init__.py, so load the module itself explicitly.
cli_module = importlib.import_module("linkvault.cli.app")

runner = CliRunner()
API = "http://lv.test"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    monkeypatch.delenv("LINKVAULT_API_URL", raising=False)
    # Wide terminal so Rich doesn't wrap table cells in assertions.
    monkeypatch.setattr(cli_module.console, "width", 200)
    monkeypatch.setattr(cli_module.err_console, "width", 200)
    yield tmp_path / "cfg"


@pytest.fixture()
def requests_seen() -> list[httpx.Request]:
    return []


@pytest.fixture()
def use_handler(
    monkeypatch: pytest.MonkeyPatch, requests_seen: list[httpx.Request]
) -> Callable[[Handler], None]:
    def install(handler: Handler) -> None:
        def recording(request: httpx.Request) -> httpx.Response:
            requests_seen.append(request)
            return handler(request)

        def factory(base_url: str, api_key: str | None) -> LinkVaultClient:
            return LinkVaultClient(
                base_url, api_key=api_key, transport=httpx.MockTransport(recording)
            )

        monkeypatch.setattr(cli_module, "make_client", factory)

    return install


def _login_stub() -> None:
    config.save_config({"api_url": API, "email": "a@example.com", "api_key": "k-123"})


def _link(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "1",
        "user_id": "u",
        "slug": "abc123",
        "destination_url": "https://example.com/",
        "expires_at": None,
        "max_clicks": None,
        "click_count": 0,
        "created_at": "2026-01-01T00:00:00",
        "deleted_at": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# config + help
# ---------------------------------------------------------------------------


def test_help_works_on_every_command() -> None:
    result = runner.invoke(cli_module.app, ["--help"])
    assert result.exit_code == 0
    for name in ("login", "logout", "shorten", "list", "info", "stats", "delete"):
        assert name in result.output
        sub = runner.invoke(cli_module.app, [name, "--help"])
        assert sub.exit_code == 0, name
        assert "Usage" in sub.output


def test_save_config_is_private(isolated_config: Path) -> None:
    path = config.save_config({"api_key": "x"})
    assert path == isolated_config / "config.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert config.get_api_key() == "x"
    assert config.clear_config() is True
    assert config.clear_config() is False


def test_resolve_api_url_precedence() -> None:
    assert config.resolve_api_url(None) == config.DEFAULT_API_URL
    config.save_config({"api_url": "http://saved:1/"})
    assert config.resolve_api_url(None) == "http://saved:1"
    assert config.resolve_api_url("http://flag:2/") == "http://flag:2"


# ---------------------------------------------------------------------------
# login / logout
# ---------------------------------------------------------------------------


def test_login_stores_key(
    use_handler: Callable[[Handler], None], requests_seen: list[httpx.Request]
) -> None:
    use_handler(lambda r: httpx.Response(200, json={"api_key": "new-key"}))
    result = runner.invoke(
        cli_module.app, ["login", "--api-url", API], input="a@example.com\nsecret1\n"
    )
    assert result.exit_code == 0, result.output
    assert "Logged in as" in result.output
    assert json.loads(requests_seen[0].content) == {
        "email": "a@example.com",
        "password": "secret1",
    }
    stored = config.load_config()
    assert stored == {"api_url": API, "email": "a@example.com", "api_key": "new-key"}


def test_login_wrong_password_prints_api_error(use_handler: Callable[[Handler], None]) -> None:
    use_handler(
        lambda r: httpx.Response(
            401, json={"detail": "Invalid email or password."}
        )
    )
    result = runner.invoke(
        cli_module.app, ["login", "--api-url", API, "--email", "a@example.com", "--password", "x"]
    )
    assert result.exit_code == 1
    assert "Invalid email or password." in result.output
    assert config.get_api_key() is None


def test_logout() -> None:
    _login_stub()
    result = runner.invoke(cli_module.app, ["logout"])
    assert result.exit_code == 0
    assert "Logged out" in result.output
    assert config.get_api_key() is None
    again = runner.invoke(cli_module.app, ["logout"])
    assert "Not logged in" in again.output


# ---------------------------------------------------------------------------
# authenticated commands
# ---------------------------------------------------------------------------


def test_commands_require_login() -> None:
    for args in (["shorten", "https://x.com"], ["list"], ["info", "a"], ["stats", "a"]):
        result = runner.invoke(cli_module.app, args)
        assert result.exit_code == 1, args
        assert "Not logged in" in result.output


def test_shorten_prints_short_url_and_sends_payload(
    use_handler: Callable[[Handler], None], requests_seen: list[httpx.Request]
) -> None:
    _login_stub()
    use_handler(lambda r: httpx.Response(201, json=_link(slug="my-slug")))
    result = runner.invoke(
        cli_module.app,
        ["shorten", "https://example.com/x", "--slug", "my-slug",
         "--expires", "2027-01-31", "--max-clicks", "5"],
    )
    assert result.exit_code == 0, result.output
    assert f"{API}/my-slug" in result.output
    req = requests_seen[0]
    assert req.headers["Authorization"] == "Bearer k-123"
    assert json.loads(req.content) == {
        "url": "https://example.com/x",
        "slug": "my-slug",
        "expires_at": "2027-01-31T23:59:59Z",
        "max_clicks": 5,
    }


def test_shorten_output_is_bold_green(
    use_handler: Callable[[Handler], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    _login_stub()
    use_handler(lambda r: httpx.Response(201, json=_link()))
    rec = Console(record=True, force_terminal=True, width=120)
    monkeypatch.setattr(cli_module, "console", rec)
    runner.invoke(cli_module.app, ["shorten", "https://example.com/"])
    assert "\x1b[1;32m" in rec.export_text(styles=True)


def test_shorten_rejects_bad_expiry() -> None:
    _login_stub()
    result = runner.invoke(cli_module.app, ["shorten", "https://x.com", "--expires", "tomorrow"])
    assert result.exit_code == 2
    assert "YYYY-MM-DD" in result.output


def test_api_error_is_reported(use_handler: Callable[[Handler], None]) -> None:
    _login_stub()
    use_handler(
        lambda r: httpx.Response(
            409, json={"error": "conflict", "detail": "Slug 'taken' is already in use."}
        )
    )
    result = runner.invoke(cli_module.app, ["shorten", "https://x.com", "--slug", "taken"])
    assert result.exit_code == 1
    assert "Slug 'taken' is already in use." in result.output


def test_connection_error_is_reported(use_handler: Callable[[Handler], None]) -> None:
    _login_stub()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    use_handler(boom)
    result = runner.invoke(cli_module.app, ["list"])
    assert result.exit_code == 1
    assert "Could not reach LinkVault" in result.output


def test_list_renders_table(
    use_handler: Callable[[Handler], None], requests_seen: list[httpx.Request]
) -> None:
    _login_stub()
    long_url = "https://example.com/" + "a" * 100
    items = [
        _link(slug="aaa111", destination_url=long_url, click_count=3),
        _link(slug="old", expires_at="2000-01-01T00:00:00"),
        _link(slug="capped", max_clicks=2, click_count=2),
    ]
    body = {"total": 3, "page": 2, "page_size": 20, "items": items}
    use_handler(lambda r: httpx.Response(200, json=body))
    result = runner.invoke(cli_module.app, ["list", "--page", "2"])
    assert result.exit_code == 0, result.output
    assert requests_seen[0].url.params["page"] == "2"
    for header in ("Slug", "Destination", "Clicks", "Expires", "Status"):
        assert header in result.output
    assert long_url not in result.output
    assert long_url[:49] + "…" in result.output
    for status in ("active", "expired", "exhausted"):
        assert status in result.output
    assert "2 / 2" in result.output


def test_list_empty(use_handler: Callable[[Handler], None]) -> None:
    _login_stub()
    body = {"total": 0, "page": 1, "page_size": 20, "items": []}
    use_handler(lambda r: httpx.Response(200, json=body))
    result = runner.invoke(cli_module.app, ["list"])
    assert result.exit_code == 0
    assert "no links yet" in result.output


def test_info(use_handler: Callable[[Handler], None]) -> None:
    _login_stub()
    use_handler(lambda r: httpx.Response(200, json=_link(click_count=7)))
    result = runner.invoke(cli_module.app, ["info", "abc123"])
    assert result.exit_code == 0, result.output
    assert f"{API}/abc123" in result.output
    assert "unlimited" in result.output
    assert "7" in result.output


def test_stats_renders_bars(
    use_handler: Callable[[Handler], None], requests_seen: list[httpx.Request]
) -> None:
    _login_stub()
    payload = {
        "slug": "abc123",
        "total_clicks": 6,
        "unique_ips": 4,
        "clicks_by_day": [
            {"date": "2026-01-01", "clicks": 2},
            {"date": "2026-01-02", "clicks": 4},
        ],
        "top_referers": [{"referer": "https://t.co", "clicks": 5}],
        "top_user_agents": [{"user_agent": "curl/8", "clicks": 6}],
    }
    use_handler(lambda r: httpx.Response(200, json=payload))
    result = runner.invoke(cli_module.app, ["stats", "abc123", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert requests_seen[0].url.params["days"] == "7"
    assert "6 clicks" in result.output and "4 unique IPs" in result.output
    assert "2026-01-02" in result.output
    assert "━" in result.output  # Rich progress-bar glyph
    assert "https://t.co" in result.output and "curl/8" in result.output


def test_stats_without_clicks(use_handler: Callable[[Handler], None]) -> None:
    _login_stub()
    payload = {"slug": "abc123", "total_clicks": 0, "unique_ips": 0,
               "clicks_by_day": [], "top_referers": [], "top_user_agents": []}
    use_handler(lambda r: httpx.Response(200, json=payload))
    result = runner.invoke(cli_module.app, ["stats", "abc123"])
    assert result.exit_code == 0
    assert "No clicks in this period" in result.output


def test_delete_requires_confirmation(
    use_handler: Callable[[Handler], None], requests_seen: list[httpx.Request]
) -> None:
    _login_stub()
    use_handler(lambda r: httpx.Response(204))
    aborted = runner.invoke(cli_module.app, ["delete", "abc123"], input="n\n")
    assert aborted.exit_code == 1
    assert requests_seen == []
    done = runner.invoke(cli_module.app, ["delete", "abc123", "--yes"])
    assert done.exit_code == 0
    assert "Deleted" in done.output
    assert requests_seen[0].method == "DELETE"


# ---------------------------------------------------------------------------
# end-to-end against the real FastAPI app
# ---------------------------------------------------------------------------


def test_end_to_end_against_real_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from linkvault.database import Base, get_db
    from linkvault.main import app as api_app

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}", poolclass=NullPool
    )
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db() -> Any:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_schema() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    api_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(api_app, base_url=API) as tc:
            tc.portal.call(create_schema)
            reg = tc.post("/users/register", json={"email": "e@example.com", "password": "secret1"})
            assert reg.status_code == 201

            monkeypatch.setattr(
                cli_module,
                "make_client",
                lambda base_url, api_key: LinkVaultClient(base_url, api_key=api_key, http=tc),
            )
            run = lambda *a: runner.invoke(cli_module.app, list(a))  # noqa: E731

            r = runner.invoke(
                cli_module.app,
                ["login", "--api-url", API, "--email", "e@example.com", "--password", "secret1"],
            )
            assert r.exit_code == 0, r.output

            r = run("shorten", "https://example.com/e2e", "--slug", "e2e-link")
            assert r.exit_code == 0, r.output
            assert f"{API}/e2e-link" in r.output

            assert tc.get("/e2e-link", follow_redirects=False).status_code in (301, 302)

            r = run("list")
            assert r.exit_code == 0 and "e2e-link" in r.output, r.output
            r = run("info", "e2e-link")
            assert r.exit_code == 0 and "https://example.com/e2e" in r.output, r.output
            r = run("stats", "e2e-link")
            assert r.exit_code == 0 and "1 clicks" in r.output, r.output

            r = run("shorten", "https://example.com/dup", "--slug", "e2e-link")
            assert r.exit_code == 1 and "already in use" in r.output, r.output

            r = run("delete", "e2e-link", "--yes")
            assert r.exit_code == 0, r.output
            r = run("info", "e2e-link")
            assert r.exit_code == 1 and "Link not found." in r.output, r.output
            tc.portal.call(engine.dispose)
    finally:
        api_app.dependency_overrides.clear()
