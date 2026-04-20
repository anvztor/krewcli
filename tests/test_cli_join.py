"""Command tests for ``krewcli join``.

These cover the legacy single-agent path, the gateway path, and the
interactive selection logic that is only reached when recipe/cookbook
flags are omitted.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from krewcli.cli import main
from krewcli.config import Settings


def _join_module():
    """Return the actual krewcli.cli.join *module* (not the Click Command).

    ``krewcli.cli.__init__`` shadows the ``join`` attribute with a Click
    Command object, so ``import krewcli.cli.join as m`` binds to the command.
    The real module is still in ``sys.modules``.
    """
    import krewcli.cli.join  # noqa: F811 — ensure it's imported
    return sys.modules["krewcli.cli.join"]


class _InitClient:
    def __init__(self, *args, **kwargs) -> None:
        self.init_args = args
        self.init_kwargs = kwargs


class _InteractiveClient:
    def __init__(self, *args, **kwargs) -> None:
        self.closed = False

    async def list_cookbooks(self):
        return [{"id": "cb_1", "name": "Cookbook One"}]

    async def get_cookbook(self, cookbook_id: str):
        assert cookbook_id == "cb_1"
        return {"recipes": [{"id": "rec_1", "name": "Recipe One"}]}

    async def close(self):
        self.closed = True


@pytest.fixture
def runner():
    return CliRunner()


def _settings(**overrides) -> Settings:
    values = {
        "krewhub_url": "http://127.0.0.1:8420",
        "krew_auth_url": "http://127.0.0.1:8421",
        "api_key": "test-key",
        "default_cookbook_id": "cb_default",
    }
    values.update(overrides)
    return Settings(**values)


class TestJoinLegacyMode:
    def test_legacy_agent_mode_resolves_and_runs_agent(self, runner, monkeypatch, tmp_path):
        captured: dict[str, object] = {}

        monkeypatch.setattr("krewcli.cli.get_settings", lambda: _settings())
        monkeypatch.setattr("krewcli.cli.KrewHubClient", _InitClient)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)
        monkeypatch.setattr("krewcli.cli.join.os.getpid", lambda: 4242)

        def _fake_resolve_mode(**kwargs):
            captured["resolve_mode"] = kwargs
            return "cli:claude", "executor", "card", "Claude Agent", ["claim"]

        async def _fake_run_agent(**kwargs):
            captured["run_agent"] = kwargs

        monkeypatch.setattr("krewcli.cli.join._resolve_mode", _fake_resolve_mode)
        monkeypatch.setattr("krewcli.cli.join._run_agent", _fake_run_agent)

        result = runner.invoke(
            main,
            [
                "join",
                "--recipe",
                "rec_1",
                "--agent",
                "claude",
                "--workdir",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Bringing agent online (legacy single-agent mode)" in result.output
        assert captured["resolve_mode"]["agent"] == "claude"
        assert captured["run_agent"]["cookbook_id"] == "cb_default"
        assert captured["run_agent"]["agent_id"] == "cli_claude_4242"
        assert captured["run_agent"]["working_dir"] == os.path.abspath(tmp_path)

    def test_legacy_mode_requires_cookbook_when_no_default(self, runner, monkeypatch):
        monkeypatch.setattr("krewcli.cli.get_settings", lambda: _settings(default_cookbook_id=""))
        monkeypatch.setattr("krewcli.cli.KrewHubClient", _InitClient)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)
        monkeypatch.setattr(
            "krewcli.cli.join._resolve_mode",
            lambda **kwargs: ("cli:claude", "executor", "card", "Claude Agent", ["claim"]),
        )

        result = runner.invoke(main, ["join", "--recipe", "rec_1", "--agent", "claude"])

        assert result.exit_code != 0
        assert "Specify --cookbook or set KREWCLI_DEFAULT_COOKBOOK_ID" in result.output


class TestJoinGatewayMode:
    def test_gateway_mode_runs_with_explicit_agents(self, runner, monkeypatch, tmp_path):
        captured: dict[str, object] = {}

        monkeypatch.setattr("krewcli.cli.get_settings", lambda: _settings())
        monkeypatch.setattr("krewcli.cli.KrewHubClient", _InitClient)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)
        monkeypatch.setattr("krewcli.cli.join.os.getpid", lambda: 5150)

        async def _fake_run_gateway(**kwargs):
            captured["run_gateway"] = kwargs

        monkeypatch.setattr("krewcli.cli.join._run_gateway", _fake_run_gateway)

        result = runner.invoke(
            main,
            [
                "join",
                "--recipe",
                "rec_1",
                "--agents",
                "claude,codex",
                "--max-concurrent",
                "2",
                "--workdir",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Starting A2A gateway" in result.output
        assert captured["run_gateway"]["recipe_id"] == "rec_1"
        assert captured["run_gateway"]["cookbook_id"] == "cb_default"
        assert captured["run_gateway"]["agent_names"] == ["claude", "codex"]
        assert captured["run_gateway"]["max_concurrent"] == 2
        assert captured["run_gateway"]["agent_id_prefix"] == "gw_5150"
        assert captured["run_gateway"]["working_dir"] == os.path.abspath(tmp_path)

    def test_interactive_mode_requires_login_when_no_session(self, runner, monkeypatch):
        monkeypatch.setattr("krewcli.cli.get_settings", lambda: _settings(default_cookbook_id=""))
        monkeypatch.setattr("krewcli.cli.KrewHubClient", _InitClient)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: None)

        result = runner.invoke(main, ["join"])

        assert result.exit_code != 0
        assert "No session. Run 'krewcli login' first." in result.output

    def test_interactive_mode_fetches_choices_and_runs_gateway(self, runner, monkeypatch, tmp_path):
        captured: dict[str, object] = {}

        monkeypatch.setattr("krewcli.cli.get_settings", lambda: _settings(default_cookbook_id=""))
        monkeypatch.setattr("krewcli.cli.KrewHubClient", _InteractiveClient)
        monkeypatch.setattr("krewcli.cli.join.KrewHubClient", _InteractiveClient)
        monkeypatch.setattr("krewcli.auth.token_store.load_token", lambda *a, **kw: "jwt-token")
        monkeypatch.setattr("krewcli.interactive.prompt_single_select", lambda title, items: 0)

        def _fake_prompt_multi_select(title, items):
            if title.startswith("Recipes"):
                return [0]
            if title.startswith("Agents"):
                return [0, 1]
            raise AssertionError(f"Unexpected prompt: {title}")

        async def _fake_run_gateway(**kwargs):
            captured["run_gateway"] = kwargs

        monkeypatch.setattr("krewcli.interactive.prompt_multi_select", _fake_prompt_multi_select)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name in {"codex", "claude"} else None)
        monkeypatch.setattr("krewcli.cli.join._run_gateway", _fake_run_gateway)
        monkeypatch.setattr("krewcli.cli.join.os.getpid", lambda: 9001)

        result = runner.invoke(main, ["join", "--workdir", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "Fetching cookbooks..." in result.output
        assert captured["run_gateway"]["cookbook_id"] == "cb_1"
        assert captured["run_gateway"]["recipe_id"] == "rec_1"
        assert captured["run_gateway"]["agent_names"] == ["codex", "claude"]
        assert captured["run_gateway"]["agent_id_prefix"] == "gw_9001"


class TestJoinHelpers:
    def test_compat_lookup_prefers_command_override(self, monkeypatch):
        import krewcli.cli as cli_mod
        join_mod = _join_module()

        sentinel = object()
        monkeypatch.setattr(cli_mod, "join_test_value", "package", raising=False)
        monkeypatch.setattr(cli_mod.join, "join_test_value", "command", raising=False)

        assert join_mod._compat_lookup("join_test_value", sentinel) == "command"

    @pytest.mark.asyncio
    async def test_run_gateway_delegates_to_runtime_module(self, monkeypatch):
        join_mod = _join_module()

        captured = {}

        async def _fake_run_gateway_impl(*args):
            captured["args"] = args

        monkeypatch.setattr(join_mod, "_run_gateway_impl", _fake_run_gateway_impl)

        settings = _settings()
        await join_mod._run_gateway(
            settings,
            "rec_1",
            "cb_1",
            "gw_1",
            "/tmp/work",
            ["claude", "codex"],
            2,
        )

        assert captured["args"] == (
            settings,
            "rec_1",
            "cb_1",
            "gw_1",
            "/tmp/work",
            ["claude", "codex"],
            2,
        )

    @pytest.mark.asyncio
    async def test_run_agent_registers_starts_server_and_cleans_up(self, monkeypatch):
        import krewcli.cli as cli_mod
        join_mod = _join_module()

        captured: dict[str, object] = {}

        class _FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                captured["client_init"] = (args, kwargs)
                captured["client"] = self

            async def register_agent(self, **kwargs):
                captured["register_agent"] = kwargs

            async def close(self):
                captured["client_closed"] = True

        class _FakeHeartbeat:
            def __init__(self, **kwargs) -> None:
                captured["heartbeat_init"] = kwargs

            def start(self):
                captured["heartbeat_started"] = True

            async def stop(self):
                captured["heartbeat_stopped"] = True

        class _FakeServer:
            def __init__(self, config) -> None:
                captured["server_config"] = config

            async def serve(self):
                captured["server_served"] = True

        def _fake_create_a2a_app(*, agent_card, executor, auth_service):
            captured["create_a2a_app"] = {
                "agent_card": agent_card,
                "executor": executor,
                "auth_service": auth_service,
            }
            return "fake-app"

        monkeypatch.setattr(cli_mod, "KrewHubClient", _FakeClient, raising=False)
        monkeypatch.setattr(cli_mod, "HeartbeatLoop", _FakeHeartbeat, raising=False)
        monkeypatch.setattr("krewcli.a2a.server.create_a2a_app", _fake_create_a2a_app)
        monkeypatch.setattr(join_mod, "_build_auth_service", lambda settings: "auth-service")
        monkeypatch.setattr(
            join_mod.uvicorn,
            "Config",
            lambda app, host, port, log_level: SimpleNamespace(
                app=app,
                host=host,
                port=port,
                log_level=log_level,
            ),
        )
        monkeypatch.setattr(join_mod.uvicorn, "Server", _FakeServer)

        settings = _settings(agent_host="127.0.0.1", agent_port=9100, heartbeat_interval=5)

        await join_mod._run_agent(
            settings=settings,
            recipe_id="rec_1",
            cookbook_id="cb_1",
            agent_id="claude_1",
            display_name="Claude Agent",
            capabilities=["claim"],
            executor="executor",
            card="card",
            working_dir="/tmp/work",
            mode="cli:claude",
            agent_name="claude",
        )

        assert captured["register_agent"]["endpoint_url"] == "http://127.0.0.1:9100"
        assert captured["heartbeat_started"] is True
        assert captured["heartbeat_stopped"] is True
        assert captured["client_closed"] is True
        assert captured["create_a2a_app"]["auth_service"] == "auth-service"
        assert captured["server_config"].app == "fake-app"
        assert captured["server_served"] is True

    @pytest.mark.asyncio
    async def test_run_agent_swallows_registration_and_cleanup_errors(self, monkeypatch):
        import krewcli.cli as cli_mod
        join_mod = _join_module()

        captured: dict[str, object] = {}

        class _FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                return None

            async def register_agent(self, **kwargs):
                captured["register_attempted"] = kwargs
                raise RuntimeError("register failed")

            async def close(self):
                captured["client_close_attempted"] = True
                raise OSError("close failed")

        class _FakeHeartbeat:
            def __init__(self, **kwargs) -> None:
                return None

            def start(self):
                captured["heartbeat_started"] = True

            async def stop(self):
                captured["heartbeat_stop_attempted"] = True
                raise OSError("stop failed")

        class _FakeServer:
            def __init__(self, config) -> None:
                return None

            async def serve(self):
                captured["server_served"] = True

        monkeypatch.setattr(cli_mod, "KrewHubClient", _FakeClient, raising=False)
        monkeypatch.setattr(cli_mod, "HeartbeatLoop", _FakeHeartbeat, raising=False)
        monkeypatch.setattr("krewcli.a2a.server.create_a2a_app", lambda **kwargs: "fake-app")
        monkeypatch.setattr(join_mod, "_build_auth_service", lambda settings: None)
        monkeypatch.setattr(
            join_mod.uvicorn,
            "Config",
            lambda app, host, port, log_level: SimpleNamespace(app=app),
        )
        monkeypatch.setattr(join_mod.uvicorn, "Server", _FakeServer)

        settings = _settings(agent_host="127.0.0.1", agent_port=9101, heartbeat_interval=5)

        await join_mod._run_agent(
            settings=settings,
            recipe_id="rec_1",
            cookbook_id="cb_1",
            agent_id="claude_2",
            display_name="Claude Agent",
            capabilities=["claim"],
            executor="executor",
            card="card",
            working_dir="/tmp/work",
            mode="cli:claude",
        )

        assert captured["heartbeat_started"] is True
        assert captured["heartbeat_stop_attempted"] is True
        assert captured["client_close_attempted"] is True
        assert captured["server_served"] is True
