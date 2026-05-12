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

        # `--no-start` is the primitive token-only path. The default
        # behaviour now mirrors multica login → daemon up; that path is
        # exercised by the daemon / up integration tests.
        result = runner.invoke(main, ["login", "--no-start"])
        assert result.exit_code == 0, result.output
        assert "Logged in as acc_test123456" in result.output
        # Both record + raw-token files written
        assert (tmp_path / "token.json").is_file()
        assert (tmp_path / "token").read_text().strip() == "jwt-test-token"


    def _stub_login_environment(self, tmp_path, monkeypatch):
        """Wire up mocks shared by both default and --foreground tests."""
        from krewcli.auth import device_flow, token_store
        from krewcli.cli import login as login_mod
        from krewcli.daemon import supervisor

        async def fake_request(_url):
            return device_flow.DeviceCode(
                device_code="dc_e2e",
                user_code="ZZZZ-0000",
                verification_uri="http://example/verify",
                expires_in=600,
            )

        async def fake_poll(_url, _device_code, *, interval=3.0, timeout=600.0):
            return device_flow.DeviceToken(
                token="jwt-e2e-token",
                account_id="acc_e2e_test",
                expires_at="2099-01-01T00:00:00Z",
            )

        monkeypatch.setattr(device_flow, "request", fake_request)
        monkeypatch.setattr(device_flow, "poll", fake_poll)
        monkeypatch.setattr(token_store, "_try_keyring", lambda: None)
        monkeypatch.setattr(token_store, "_DEFAULT_DIR", tmp_path)
        # Isolate supervisor state from the developer's real ~/.krewcli.
        monkeypatch.setattr(supervisor, "_DEFAULT_DIR", tmp_path / ".krewcli")

        # Force the autodetect set: only `claude` looks installed. We
        # short-circuit `which` to avoid leaking the developer's real
        # PATH (claude/codex/gemini all installed on this box).
        monkeypatch.setattr(
            login_mod.shutil,
            "which",
            lambda name: "/fake/claude" if name == "claude" else None,
        )

        class _FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class _FakeHTTP:
            def __init__(self):
                self.posts: list[tuple[str, dict]] = []

            async def post(self, url, json=None):
                self.posts.append((url, json or {}))
                return _FakeResp({
                    "cookbook": {"id": "cb_e2e", "name": "my-cookbook"},
                })

            class _BaseURL:
                def __str__(self):  # noqa: D401 - mimic httpx URL
                    return "http://krewhub.fake/"

            base_url = _BaseURL()

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                self._client = _FakeHTTP()
                self.calls: list[str] = []

            async def list_cookbooks(self):
                self.calls.append("list_cookbooks")
                return []

            async def get_cookbook(self, cb_id):
                self.calls.append(f"get_cookbook:{cb_id}")
                return {"cookbook": {"id": cb_id}}

            async def close(self):
                self.calls.append("close")

        captured: dict = {}

        def fake_make_sync_client(_settings):
            client = _FakeClient()
            captured["client"] = client
            return client

        monkeypatch.setattr(login_mod, "_make_sync_client", fake_make_sync_client)
        return captured, login_mod, supervisor

    def test_login_foreground_runs_daemon_inline(self, runner, tmp_path, monkeypatch):
        """`krewcli login --foreground` resolves cookbook/recipe and hands
        off to `_run_daemon` with the resolved config — same chain as
        `krewcli up` but driven by login.
        """
        captured, login_mod, _supervisor = self._stub_login_environment(
            tmp_path, monkeypatch,
        )

        async def fake_run_daemon(**kwargs):
            captured["daemon_kwargs"] = kwargs

        monkeypatch.setattr(login_mod, "_run_daemon", fake_run_daemon)

        result = runner.invoke(
            main, ["login", "--foreground", "--workdir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "Logged in as acc_e2e_test" in result.output
        assert "agents online" in result.output

        client = captured["client"]
        assert "list_cookbooks" in client.calls
        assert any(url.endswith("/cookbooks") for url, _ in client._client.posts)
        kw = captured["daemon_kwargs"]
        assert kw["cookbook_id"] == "cb_e2e"
        assert "claude" in kw["backends"]
        assert kw["working_dir"] == str(tmp_path)

    def test_login_default_spawns_background_daemon(self, runner, tmp_path, monkeypatch):
        """Default `krewcli login` mirrors multica login → daemon up.

        Forks a detached child via supervisor.spawn_detached and exits
        cleanly. We mock spawn_detached + wait_until_ready to verify the
        chain without actually launching a child Python process.
        """
        captured, _login_mod, supervisor_mod = self._stub_login_environment(
            tmp_path, monkeypatch,
        )

        spawn_calls: list[list[str]] = []

        def fake_spawn(args):
            spawn_calls.append(list(args))
            return 424242

        monkeypatch.setattr(supervisor_mod, "spawn_detached", fake_spawn)
        monkeypatch.setattr(
            supervisor_mod, "wait_until_ready", lambda pid, **_: True,
        )

        result = runner.invoke(main, ["login", "--workdir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "Logged in as acc_e2e_test" in result.output
        assert "agents online" in result.output
        assert "Daemon pid: 424242" in result.output

        # Spawn was invoked exactly once with the resolved foreground args.
        assert len(spawn_calls) == 1
        argv = spawn_calls[0]
        assert "--foreground" in argv
        assert argv[argv.index("--cookbook") + 1] == "cb_e2e"
        assert argv[argv.index("--agents") + 1] == "claude"
        assert argv[argv.index("--workdir") + 1] == str(tmp_path)

        # Status sidecar was seeded so `daemon status` works immediately.
        status_path = supervisor_mod.status_path()
        assert status_path.is_file()

    def test_login_always_runs_fresh_device_flow(self, runner, tmp_path, monkeypatch):
        """Each `krewcli login` invocation runs a fresh device flow,
        even when a token is already on disk.

        Bug history: an earlier version cached the JWT and silently
        returned without pairing, but the agent JWT is sandboxed to
        one daemon session — rotating it on every pairing is good
        security hygiene, and the human's web session persists via
        cookie at auth.cookrew.dev so the operator doesn't re-enter
        credentials.
        """
        import jwt as _jwt
        from krewcli.auth import device_flow, token_store
        from krewcli.cli import login as login_mod

        monkeypatch.setattr(token_store, "_try_keyring", lambda: None)
        monkeypatch.setattr(token_store, "_DEFAULT_DIR", tmp_path)

        # Seed a token that the legacy cache check would have reused.
        stale_token = _jwt.encode(
            {"sub": "acc_stale", "exp": 9999999999},
            "x", algorithm="HS256",
        )
        token_store.save_record({
            "token": stale_token,
            "account_id": "acc_stale",
            "expires_at": "2099-01-01T00:00:00Z",
        })
        token_store.save_token(stale_token)

        request_calls: list = []

        async def fake_request(url):
            request_calls.append(url)
            return device_flow.DeviceCode(
                device_code="dc_fresh",
                user_code="FRSH-0000",
                verification_uri="x",
                expires_in=600,
            )

        async def fake_poll(_url, _device_code, *, interval=3.0, timeout=600.0):
            return device_flow.DeviceToken(
                token="jwt-fresh",
                account_id="acc_fresh",
                expires_at="2099-01-01T00:00:00Z",
            )

        monkeypatch.setattr(login_mod.device_flow, "request", fake_request)
        monkeypatch.setattr(login_mod.device_flow, "poll", fake_poll)

        result = runner.invoke(main, ["login", "--no-start"])
        assert result.exit_code == 0, result.output
        assert "Logged in as acc_fresh" in result.output
        assert len(request_calls) == 1, "device_flow.request must run on every login"

    def test_login_idempotent_when_daemon_already_running(self, runner, tmp_path, monkeypatch):
        """If a daemon is already running, login skips the bootstrap +
        spawn and just acknowledges the running state. Mirrors
        multica's syncToken-without-restart path.
        """
        captured, _login_mod, supervisor_mod = self._stub_login_environment(
            tmp_path, monkeypatch,
        )

        monkeypatch.setattr(
            supervisor_mod,
            "read_status",
            lambda: {
                "pid": 99999,
                "alive": True,
                "agents": ["claude", "codex"],
                "cookbook_id": "cb_x",
                "recipe_id": "rec_x",
            },
        )

        spawn_called: list = []

        monkeypatch.setattr(
            supervisor_mod,
            "spawn_detached",
            lambda args: spawn_called.append(args) or 0,
        )

        result = runner.invoke(main, ["login", "--workdir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "Daemon already running (pid 99999)" in result.output
        assert "Agents: claude, codex" in result.output
        # Bootstrap (cookbook/recipe resolution) was skipped — no list_cookbooks.
        assert "client" not in captured
        # No new daemon was spawned.
        assert spawn_called == []


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
