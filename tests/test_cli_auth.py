"""Tests for CLI wallet and SIWE login commands."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from krewcli.cli import main


@pytest.fixture
def runner():
    return CliRunner()


class TestWalletCommands:
    def test_wallet_create(self, runner, tmp_path):
        with patch("krewcli.auth.wallet._DEFAULT_DIR", tmp_path):
            result = runner.invoke(main, ["wallet", "create"])

        assert result.exit_code == 0
        assert "Wallet created: 0x" in result.output
        assert (tmp_path / "wallet").is_file()

    def test_wallet_import_valid(self, runner, tmp_path):
        # A valid private key (32 bytes hex)
        key = "0x" + "ab" * 32

        with patch("krewcli.auth.wallet._DEFAULT_DIR", tmp_path):
            result = runner.invoke(main, ["wallet", "import", key])

        assert result.exit_code == 0
        assert "Wallet imported: 0x" in result.output

    def test_wallet_import_invalid(self, runner):
        result = runner.invoke(main, ["wallet", "import", "not-a-key"])
        assert result.exit_code == 1
        assert "Invalid" in result.output

    def test_wallet_address(self, runner, tmp_path):
        # Create a wallet first
        with patch("krewcli.auth.wallet._DEFAULT_DIR", tmp_path):
            runner.invoke(main, ["wallet", "create"])
            result = runner.invoke(main, ["wallet", "address"])

        assert result.exit_code == 0
        assert result.output.strip().startswith("0x")

    def test_wallet_address_missing(self, runner, tmp_path):
        with patch("krewcli.auth.wallet._DEFAULT_DIR", tmp_path):
            result = runner.invoke(main, ["wallet", "address"])

        assert result.exit_code == 1
        assert "No wallet found" in result.output


class TestLoginCommand:
    def test_login_device_flow_approved(self, runner, tmp_path):
        """Device flow: request code → poll → approved → JWT saved."""
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        # First call: POST /device/request
        request_resp = MagicMock()
        request_resp.status_code = 200
        request_resp.raise_for_status = MagicMock()
        request_resp.json.return_value = {
            "device_code": "dc_test",
            "user_code": "ABCD-1234",
            "expires_in": 600,
        }

        # Second call: POST /device/token → approved immediately
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {
            "status": "approved",
            "token": "jwt-test-token",
            "account_id": "acc_test123456",
            "wallet_address": "0xABC123",
            "session_id": "ses_test",
            "expires_at": "2026-04-10T00:00:00Z",
        }

        mock_client.post.side_effect = [request_resp, token_resp]

        with patch("krewcli.cli.httpx.Client", return_value=mock_client), \
             patch("time.sleep"), \
             patch("webbrowser.open"), \
             patch("krewcli.auth.token_store._DEFAULT_DIR", tmp_path):
            result = runner.invoke(main, ["login"])

        assert result.exit_code == 0
        assert "Logged in as acc_test123456" in result.output
        assert (tmp_path / "token").read_text().strip() == "jwt-test-token"

    def test_login_connection_error(self, runner):
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = httpx.ConnectError("refused")

        with patch("krewcli.cli.httpx.Client", return_value=mock_client), \
             patch("webbrowser.open"):
            result = runner.invoke(main, ["login"])

        assert result.exit_code == 1
        assert "Could not connect" in result.output or "krewauth" in result.output
