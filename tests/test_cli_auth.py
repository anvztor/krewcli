"""Tests for CLI register and login commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from krewcli.cli import main


@pytest.fixture
def runner():
    return CliRunner()


class TestRegisterCommand:
    def test_register_success(self, runner, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"email": "u@b.com", "id": "user_1", "is_active": True, "roles": []}

        mock_client_instance = MagicMock()
        mock_client_instance.post.return_value = mock_resp
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)

        with patch("krewcli.cli.httpx.Client", return_value=mock_client_instance):
            result = runner.invoke(main, ["register"], input="u@b.com\npassword1\npassword1\n")

        assert result.exit_code == 0
        assert "Registered as u@b.com" in result.output

    def test_register_server_error(self, runner):
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.json.return_value = {"error": "Email already registered"}

        mock_client_instance = MagicMock()
        mock_client_instance.post.return_value = mock_resp
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)

        with patch("krewcli.cli.httpx.Client", return_value=mock_client_instance):
            result = runner.invoke(main, ["register"], input="u@b.com\npassword1\npassword1\n")

        assert result.exit_code == 1
        assert "Email already registered" in result.output

    def test_register_connection_error(self, runner):
        import httpx

        mock_client_instance = MagicMock()
        mock_client_instance.post.side_effect = httpx.ConnectError("refused")
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)

        with patch("krewcli.cli.httpx.Client", return_value=mock_client_instance):
            result = runner.invoke(main, ["register"], input="u@b.com\npassword1\npassword1\n")

        assert result.exit_code == 1
        assert "Could not connect" in result.output


class TestLoginCommand:
    def test_login_success(self, runner, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt-tok", "token_type": "bearer"}

        mock_client_instance = MagicMock()
        mock_client_instance.post.return_value = mock_resp
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)

        with patch("krewcli.cli.httpx.Client", return_value=mock_client_instance), \
             patch("krewcli.cli.save_token") as mock_save:
            result = runner.invoke(main, ["login"], input="u@b.com\npassword1\n")

        assert result.exit_code == 0
        assert "Logged in" in result.output
        mock_save.assert_called_once_with("jwt-tok")

    def test_login_bad_credentials(self, runner):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"error": "Invalid credentials"}

        mock_client_instance = MagicMock()
        mock_client_instance.post.return_value = mock_resp
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)

        with patch("krewcli.cli.httpx.Client", return_value=mock_client_instance):
            result = runner.invoke(main, ["login"], input="u@b.com\nwrong\n")

        assert result.exit_code == 1
        assert "Invalid credentials" in result.output

    def test_login_connection_error(self, runner):
        import httpx

        mock_client_instance = MagicMock()
        mock_client_instance.post.side_effect = httpx.ConnectError("refused")
        mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
        mock_client_instance.__exit__ = MagicMock(return_value=False)

        with patch("krewcli.cli.httpx.Client", return_value=mock_client_instance):
            result = runner.invoke(main, ["login"], input="u@b.com\npassword1\n")

        assert result.exit_code == 1
        assert "Could not connect" in result.output
