"""Tests for the LinkVault CLI."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from linkvault.cli.app import app

runner = CliRunner()


@pytest.fixture
def temp_config_dir(tmp_path):
    """Temporarily override the config directory."""
    with patch("linkvault.cli.app.CONFIG_DIR", tmp_path):
        with patch("linkvault.cli.app.CONFIG_FILE", tmp_path / "config.json"):
            yield tmp_path


class TestLogin:
    """Tests for the login command."""

    def test_login_success(self, temp_config_dir):
        """Test successful login."""
        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.json.return_value = {"api_key": "test-key-123"}
            mock_client.post.return_value = mock_response

            result = runner.invoke(
                app,
                ["login"],
                input="test@example.com\npassword123\n",
            )

            assert result.exit_code == 0
            assert "Logged in as" in result.stdout
            assert "test@example.com" in result.stdout

            # Verify config was saved
            config_file = temp_config_dir / "config.json"
            assert config_file.exists()
            config = json.loads(config_file.read_text())
            assert config["email"] == "test@example.com"
            assert config["api_key"] == "test-key-123"

    def test_login_invalid_credentials(self, temp_config_dir):
        """Test login with invalid credentials."""
        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_response.raise_for_status.side_effect = Exception("401 Unauthorized")
            mock_client.post.return_value = mock_response

            result = runner.invoke(
                app,
                ["login"],
                input="test@example.com\nwrongpassword\n",
            )

            assert result.exit_code != 0

    def test_login_connection_error(self, temp_config_dir):
        """Test login with connection error."""
        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_client.post.side_effect = Exception("Connection refused")

            result = runner.invoke(
                app,
                ["login"],
                input="test@example.com\npassword123\n",
            )

            assert result.exit_code != 0


class TestLogout:
    """Tests for the logout command."""

    def test_logout_success(self, temp_config_dir):
        """Test successful logout."""
        # Create a config file first
        config_file = temp_config_dir / "config.json"
        config_file.write_text(json.dumps({"email": "test@example.com", "api_key": "test-key"}))

        result = runner.invoke(app, ["logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.stdout
        assert not config_file.exists()

    def test_logout_not_logged_in(self, temp_config_dir):
        """Test logout when not logged in."""
        result = runner.invoke(app, ["logout"])

        assert result.exit_code == 0
        assert "Not logged in" in result.stdout


class TestShorten:
    """Tests for the shorten command."""

    def test_shorten_success(self, temp_config_dir):
        """Test successful link creation."""
        # Setup login
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "id": "link-1",
                "slug": "abc123",
                "destination_url": "https://example.com",
                "click_count": 0,
                "created_at": "2025-01-15T10:00:00",
                "expires_at": None,
                "max_clicks": None,
            }
            mock_client.post.return_value = mock_response

            result = runner.invoke(
                app,
                ["shorten", "https://example.com"],
            )

            assert result.exit_code == 0
            assert "Link created" in result.stdout
            assert "abc123" in result.stdout
            assert "https://example.com" in result.stdout

    def test_shorten_with_custom_slug(self, temp_config_dir):
        """Test link creation with custom slug."""
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "id": "link-1",
                "slug": "myslug",
                "destination_url": "https://example.com",
                "click_count": 0,
                "created_at": "2025-01-15T10:00:00",
                "expires_at": None,
                "max_clicks": None,
            }
            mock_client.post.return_value = mock_response

            result = runner.invoke(
                app,
                ["shorten", "https://example.com", "--slug", "myslug"],
            )

            assert result.exit_code == 0
            assert "myslug" in result.stdout

    def test_shorten_with_expiry(self, temp_config_dir):
        """Test link creation with expiry date."""
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "id": "link-1",
                "slug": "abc123",
                "destination_url": "https://example.com",
                "click_count": 0,
                "created_at": "2025-01-15T10:00:00",
                "expires_at": "2025-02-15T00:00:00",
                "max_clicks": None,
            }
            mock_client.post.return_value = mock_response

            result = runner.invoke(
                app,
                ["shorten", "https://example.com", "--expires", "2025-02-15"],
            )

            assert result.exit_code == 0
            assert "Expires" in result.stdout

    def test_shorten_not_logged_in(self, temp_config_dir):
        """Test shorten when not logged in."""
        result = runner.invoke(
            app,
            ["shorten", "https://example.com"],
        )

        assert result.exit_code != 0
        assert "Not logged in" in result.stdout


class TestList:
    """Tests for the list command."""

    def test_list_success(self, temp_config_dir):
        """Test listing links."""
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "total": 2,
                "page": 1,
                "page_size": 20,
                "items": [
                    {
                        "slug": "abc123",
                        "destination_url": "https://example.com",
                        "click_count": 5,
                        "created_at": "2025-01-15T10:00:00",
                        "expires_at": None,
                    },
                    {
                        "slug": "def456",
                        "destination_url": "https://google.com",
                        "click_count": 10,
                        "created_at": "2025-01-14T10:00:00",
                        "expires_at": "2025-02-15T00:00:00",
                    },
                ],
            }
            mock_client.get.return_value = mock_response

            result = runner.invoke(app, ["list"])

            assert result.exit_code == 0
            assert "abc123" in result.stdout
            assert "def456" in result.stdout
            assert "5" in result.stdout
            assert "10" in result.stdout

    def test_list_empty(self, temp_config_dir):
        """Test listing when no links exist."""
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "total": 0,
                "page": 1,
                "page_size": 20,
                "items": [],
            }
            mock_client.get.return_value = mock_response

            result = runner.invoke(app, ["list"])

            assert result.exit_code == 0
            assert "No links found" in result.stdout


class TestInfo:
    """Tests for the info command."""

    def test_info_success(self, temp_config_dir):
        """Test getting link info."""
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "slug": "abc123",
                "destination_url": "https://example.com",
                "click_count": 5,
                "created_at": "2025-01-15T10:00:00",
                "expires_at": "2025-02-15T00:00:00",
                "max_clicks": 100,
            }
            mock_client.get.return_value = mock_response

            result = runner.invoke(app, ["info", "abc123"])

            assert result.exit_code == 0
            assert "abc123" in result.stdout
            assert "https://example.com" in result.stdout
            assert "5" in result.stdout

    def test_info_not_found(self, temp_config_dir):
        """Test info for non-existent link."""
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.raise_for_status.side_effect = Exception("404 Not Found")
            mock_client.get.return_value = mock_response

            result = runner.invoke(app, ["info", "nonexistent"])

            assert result.exit_code != 0


class TestStats:
    """Tests for the stats command."""

    def test_stats_success(self, temp_config_dir):
        """Test getting link stats."""
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "slug": "abc123",
                "total_clicks": 15,
                "unique_ips": 5,
                "clicks_by_day": [
                    {"date": "2025-01-15", "clicks": 5},
                    {"date": "2025-01-14", "clicks": 10},
                ],
                "top_referers": [
                    {"referer": "https://twitter.com", "clicks": 8},
                    {"referer": "https://reddit.com", "clicks": 7},
                ],
                "top_user_agents": [
                    {"user_agent": "Mozilla/5.0", "clicks": 10},
                    {"user_agent": "curl/7.0", "clicks": 5},
                ],
            }
            mock_client.get.return_value = mock_response

            result = runner.invoke(app, ["stats", "abc123"])

            assert result.exit_code == 0
            assert "abc123" in result.stdout
            assert "15" in result.stdout
            assert "5" in result.stdout


class TestDelete:
    """Tests for the delete command."""

    def test_delete_success(self, temp_config_dir):
        """Test deleting a link."""
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_client.delete.return_value = mock_response

            result = runner.invoke(
                app,
                ["delete", "abc123", "--force"],
            )

            assert result.exit_code == 0
            assert "deleted" in result.stdout

    def test_delete_with_confirmation(self, temp_config_dir):
        """Test delete with confirmation prompt."""
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value.__enter__.return_value = mock_client
            mock_response = MagicMock()
            mock_client.delete.return_value = mock_response

            result = runner.invoke(
                app,
                ["delete", "abc123"],
                input="y\n",
            )

            assert result.exit_code == 0
            assert "deleted" in result.stdout

    def test_delete_cancelled(self, temp_config_dir):
        """Test delete cancelled by user."""
        config_file = temp_config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "email": "test@example.com",
                "api_key": "test-key-123",
                "api_url": "http://localhost:8000",
            })
        )

        result = runner.invoke(
            app,
            ["delete", "abc123"],
            input="n\n",
        )

        assert result.exit_code == 0
        assert "Cancelled" in result.stdout
