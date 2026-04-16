"""Unit tests for ``krewcli.cli._resolve_mode``.

These focus on the flag-to-executor wiring used by ``krewcli join``.
Heavy executor internals are mocked where needed so the tests stay fast
and validate only the CLI resolution logic.
"""

from __future__ import annotations

from types import SimpleNamespace

import click
import pytest

from krewcli.a2a.executors.cli_agent import CLIExecutor
from krewcli.a2a.executors.remote_agent import RemoteExecutor
from krewcli.cli import _default_model, _resolve_mode
from krewcli.config import Settings


def _settings() -> Settings:
    return Settings(
        krewhub_url="http://127.0.0.1:8420",
        api_key="test-key",
        default_cookbook_id="cb_test",
        verify_ssl=False,
    )


def _resolve(**overrides):
    kwargs = dict(
        agent=None,
        provider=None,
        model=None,
        framework=None,
        endpoint=None,
        orchestrator=False,
        host="127.0.0.1",
        port=9999,
        working_dir="/tmp/work",
        settings=_settings(),
    )
    kwargs.update(overrides)
    return _resolve_mode(**kwargs)


class TestAgentBranch:
    def test_agent_returns_cli_executor_and_registry_metadata(self):
        mode, executor, card, display_name, caps = _resolve(agent="claude")

        assert mode == "cli:claude"
        assert isinstance(executor, CLIExecutor)
        assert display_name == "Claude Agent"
        assert "claim" in caps
        assert card.name == "cli:claude"
        assert card.url == "http://127.0.0.1:9999"


class TestProviderBranch:
    def test_provider_uses_default_model(self, monkeypatch):
        class _FakeExecutor:
            def __init__(self, model: str) -> None:
                self.model = model

        fake_card = SimpleNamespace(name="llm:anthropic", url="http://127.0.0.1:9999")
        monkeypatch.setattr("krewcli.a2a.executors.direct_llm.DirectLLMExecutor", _FakeExecutor)
        monkeypatch.setattr(
            "krewcli.a2a.executors.direct_llm.build_direct_llm_card",
            lambda provider, host, port: fake_card,
        )

        mode, executor, card, display_name, caps = _resolve(provider="anthropic")

        assert mode == "llm:anthropic"
        assert executor.model == "anthropic:claude-sonnet-4-20250514"
        assert card is fake_card
        assert display_name == "LLM (anthropic)"
        assert caps == ["summarize", "classify", "plan", "review"]

    def test_provider_uses_explicit_model_override(self, monkeypatch):
        class _FakeExecutor:
            def __init__(self, model: str) -> None:
                self.model = model

        monkeypatch.setattr("krewcli.a2a.executors.direct_llm.DirectLLMExecutor", _FakeExecutor)
        monkeypatch.setattr(
            "krewcli.a2a.executors.direct_llm.build_direct_llm_card",
            lambda provider, host, port: SimpleNamespace(name="x", url=f"http://{host}:{port}"),
        )

        _mode, executor, _card, _display_name, _caps = _resolve(
            provider="openai",
            model="gpt-4.1-mini",
        )

        assert executor.model == "openai:gpt-4.1-mini"


class TestFrameworkBranch:
    def test_framework_passes_model_and_workdir(self, monkeypatch):
        class _FakeExecutor:
            def __init__(self, model: str, working_dir: str) -> None:
                self.model = model
                self.working_dir = working_dir

        fake_card = SimpleNamespace(name="framework:openai", url="http://127.0.0.1:9999")
        monkeypatch.setattr("krewcli.a2a.executors.framework_agent.FrameworkExecutor", _FakeExecutor)
        monkeypatch.setattr(
            "krewcli.a2a.executors.framework_agent.build_framework_card",
            lambda provider, host, port: fake_card,
        )

        mode, executor, card, display_name, caps = _resolve(
            framework="openai",
            working_dir="/repo",
        )

        assert mode == "framework:openai"
        assert executor.model == "openai:gpt-4o"
        assert executor.working_dir == "/repo"
        assert card is fake_card
        assert display_name == "Framework (openai)"
        assert caps == ["code", "implement", "fix", "test"]


class TestEndpointBranch:
    def test_endpoint_returns_remote_executor(self):
        mode, executor, card, display_name, caps = _resolve(
            endpoint="http://remote/api",
        )

        assert mode == "remote"
        assert isinstance(executor, RemoteExecutor)
        assert display_name == "Remote (http://remote/api)"
        assert caps == ["code"]
        assert card.name == "remote:http://remote/api"


class TestOrchestratorBranch:
    def test_orchestrator_uses_planner_executor_and_hub_client(self, monkeypatch):
        captured: dict[str, object] = {}

        class _FakeClient:
            def __init__(self, base_url: str, api_key: str, verify_ssl: bool) -> None:
                captured["client_args"] = (base_url, api_key, verify_ssl)

        class _FakeExecutor:
            def __init__(self, *, krewhub_client, cookbook_id: str) -> None:
                self.krewhub_client = krewhub_client
                self.cookbook_id = cookbook_id

        fake_card = SimpleNamespace(
            name="planner",
            url="http://0.0.0.0:12345",
            skills=[SimpleNamespace(id="generate-graph")],
        )

        monkeypatch.setattr("krewcli.cli.KrewHubClient", _FakeClient)
        monkeypatch.setattr(
            "krewcli.a2a.executors.planner_agent.PlannerOrchestratorExecutor",
            _FakeExecutor,
        )
        monkeypatch.setattr(
            "krewcli.a2a.executors.planner_agent.build_planner_card",
            lambda host, port: fake_card,
        )

        mode, executor, card, display_name, caps = _resolve(
            orchestrator=True,
            host="0.0.0.0",
            port=12345,
        )

        assert mode == "orchestrator"
        assert executor.cookbook_id == "cb_test"
        assert captured["client_args"] == ("http://127.0.0.1:8420", "test-key", False)
        assert card is fake_card
        assert display_name == "Planner"
        assert caps == ["generate-graph"]


class TestErrorsAndHelpers:
    def test_no_mode_raises_usage_error(self):
        with pytest.raises(click.UsageError) as exc_info:
            _resolve()

        msg = str(exc_info.value)
        assert "--agent" in msg
        assert "--provider" in msg
        assert "--framework" in msg
        assert "--endpoint" in msg
        assert "--orchestrator" in msg

    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("anthropic", "claude-sonnet-4-20250514"),
            ("openai", "gpt-4o"),
            ("unknown", "claude-sonnet-4-20250514"),
        ],
    )
    def test_default_model(self, provider, expected):
        assert _default_model(provider) == expected
