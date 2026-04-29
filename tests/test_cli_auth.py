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


class TestSessionKeyCommands:
    def test_session_key_create(self, runner, tmp_path):
        with patch("krewcli.session_key._DEFAULT_DIR", tmp_path):
            result = runner.invoke(main, ["session-key", "create"])

        assert result.exit_code == 0
        assert "Session key created: 0x" in result.output
        assert (tmp_path / "session_key").is_file()

    def test_session_key_address(self, runner, tmp_path):
        with patch("krewcli.session_key._DEFAULT_DIR", tmp_path):
            runner.invoke(main, ["session-key", "create"])
            result = runner.invoke(main, ["session-key", "address"])

        assert result.exit_code == 0
        assert result.output.strip().startswith("0x")

    def test_session_key_address_missing(self, runner, tmp_path):
        with patch("krewcli.session_key._DEFAULT_DIR", tmp_path):
            result = runner.invoke(main, ["session-key", "address"])

        assert result.exit_code == 1
        assert "No session key" in result.output


class TestLoginCommand:
    """Track A1: ``krewcli login`` runs the inverted device-flow."""

    def test_login_invokes_device_flow_and_saves_record(self, runner, tmp_path, monkeypatch):
        from krewcli.auth import device_flow, token_store

        async def fake_request(_url):
            return device_flow.DeviceCode(
                device_code="dc_test",
                user_code="ABCD-1234",
                verification_uri="http://example/verify?code=ABCD-1234",
                expires_in=600,
            )

        async def fake_poll(_url, _device_code, *, interval=3.0, timeout=600.0):
            return device_flow.DeviceToken(
                token="jwt-test-token",
                account_id="acc_test123456",
                expires_at="2026-04-10T00:00:00Z",
            )

        monkeypatch.setattr(device_flow, "request", fake_request)
        monkeypatch.setattr(device_flow, "poll", fake_poll)
        # Force file fallback by disabling keyring lookup in test
        monkeypatch.setattr(token_store, "_try_keyring", lambda: None)
        monkeypatch.setattr(token_store, "_DEFAULT_DIR", tmp_path)

        result = runner.invoke(main, ["login"])
        assert result.exit_code == 0, result.output
        assert "Logged in as acc_test123456" in result.output
        # Both record + raw-token files written
        assert (tmp_path / "token.json").is_file()
        assert (tmp_path / "token").read_text().strip() == "jwt-test-token"


class TestLogoutCommand:
    def test_logout_clears_record(self, runner, tmp_path, monkeypatch):
        from krewcli.auth import token_store

        monkeypatch.setattr(token_store, "_try_keyring", lambda: None)
        monkeypatch.setattr(token_store, "_DEFAULT_DIR", tmp_path)
        token_store.save_record({
            "token": "x",
            "account_id": "acc_x",
            "expires_at": "2099-01-01T00:00:00Z",
        })
        token_store.save_token("x")
        result = runner.invoke(main, ["logout"])
        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not (tmp_path / "token").is_file()
        assert not (tmp_path / "token.json").is_file()


class TestWhoamiCommand:
    def test_whoami_when_logged_out(self, runner, tmp_path, monkeypatch):
        from krewcli.auth import token_store

        monkeypatch.setattr(token_store, "_try_keyring", lambda: None)
        monkeypatch.setattr(token_store, "_DEFAULT_DIR", tmp_path)
        result = runner.invoke(main, ["whoami"])
        assert result.exit_code == 0
        assert "Not logged in" in result.output

    def test_whoami_decodes_record(self, runner, tmp_path, monkeypatch):
        import jwt as _jwt
        from krewcli.auth import token_store

        monkeypatch.setattr(token_store, "_try_keyring", lambda: None)
        monkeypatch.setattr(token_store, "_DEFAULT_DIR", tmp_path)
        token = _jwt.encode(
            {"sub": "acc_alice", "auth_method": "device", "exp": 9999999999},
            "irrelevant",
            algorithm="HS256",
        )
        token_store.save_record({
            "token": token, "account_id": "acc_alice", "expires_at": "x",
        })
        result = runner.invoke(main, ["whoami"])
        assert result.exit_code == 0
        assert "acc_alice" in result.output
        assert "device" in result.output


# Keep `httpx` reachable so the lint in this test module doesn't break with
# unused imports if a future change re-introduces synchronous flows.
_httpx_module = httpx
