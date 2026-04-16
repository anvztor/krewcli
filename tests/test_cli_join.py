"""Command tests for ``krewcli join``.

These cover the legacy single-agent path, the gateway path, and the
interactive selection logic that is only reached when recipe/cookbook
flags are omitted.
"""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from krewcli.cli import main
from krewcli.config import Settings


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
        monkeypatch.setattr("krewcli.cli.os.getpid", lambda: 4242)

        def _fake_resolve_mode(**kwargs):
            captured["resolve_mode"] = kwargs
            return "cli:claude", "executor", "card", "Claude Agent", ["claim"]

        async def _fake_run_agent(**kwargs):
            captured["run_agent"] = kwargs

        monkeypatch.setattr("krewcli.cli._resolve_mode", _fake_resolve_mode)
        monkeypatch.setattr("krewcli.cli._run_agent", _fake_run_agent)

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
            "krewcli.cli._resolve_mode",
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
        monkeypatch.setattr("krewcli.cli.os.getpid", lambda: 5150)

        async def _fake_run_gateway(**kwargs):
            captured["run_gateway"] = kwargs

        monkeypatch.setattr("krewcli.cli._run_gateway", _fake_run_gateway)

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
        monkeypatch.setattr("krewcli.cli._run_gateway", _fake_run_gateway)
        monkeypatch.setattr("krewcli.cli.os.getpid", lambda: 9001)

        result = runner.invoke(main, ["join", "--workdir", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "Fetching cookbooks..." in result.output
        assert captured["run_gateway"]["cookbook_id"] == "cb_1"
        assert captured["run_gateway"]["recipe_id"] == "rec_1"
        assert captured["run_gateway"]["agent_names"] == ["codex", "claude"]
        assert captured["run_gateway"]["agent_id_prefix"] == "gw_9001"
