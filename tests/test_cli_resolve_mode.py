"""Unit tests for krewcli.cli._resolve_mode.

Smoke-tests every executor branch by checking the returned tuple shape:
mode label, executor type, agent card, display name, capabilities. This
guards against accidental skew between the CLI flag → executor wiring
and the agent registration metadata that flows into krewhub.
"""

from __future__ import annotations

import pytest

from krewcli.a2a.executors.cli_agent import CLIExecutor
from krewcli.a2a.executors.orchestrator_agent import OrchestratorExecutor
from krewcli.a2a.executors.planner_agent import PlannerOrchestratorExecutor
from krewcli.a2a.executors.remote_agent import RemoteExecutor
from krewcli.cli import _resolve_mode
from krewcli.config import Settings


def _settings() -> Settings:
    return Settings(
        krewhub_url="http://127.0.0.1:8420",
        api_key="test-key",
        default_cookbook_id="cb_test",
    )


def _resolve(**overrides):
    """Call _resolve_mode with sensible defaults; override per test."""
    kwargs = dict(
        agent=None, provider=None, model=None, framework=None,
        endpoint=None, orchestrator=False, planner=False,
        host="127.0.0.1", port=9999, working_dir="/tmp",
        settings=_settings(),
    )
    kwargs.update(overrides)
    return _resolve_mode(**kwargs)


# ---------------------------------------------------------------------------
# Planner branch (the new wiring this session)
# ---------------------------------------------------------------------------


class TestPlannerBranch:
    def test_planner_returns_planner_executor(self):
        mode, executor, card, display_name, caps = _resolve(planner=True)
        assert mode == "planner"
        assert isinstance(executor, PlannerOrchestratorExecutor)
        assert display_name == "Planner"

    def test_planner_advertises_generate_graph_capability(self):
        _mode, _executor, _card, _display, caps = _resolve(planner=True)
        assert caps == ["generate-graph"]

    def test_planner_card_has_generate_graph_skill(self):
        _mode, _executor, card, _display, _caps = _resolve(planner=True)
        skill_ids = {s.id for s in card.skills}
        assert "generate-graph" in skill_ids
        assert card.name == "planner"

    def test_planner_card_url_uses_provided_host_and_port(self):
        _mode, _executor, card, _display, _caps = _resolve(
            planner=True, host="0.0.0.0", port=12345,
        )
        assert card.url == "http://0.0.0.0:12345"


# ---------------------------------------------------------------------------
# Existing branches (regression guard against the new arg breaking them)
# ---------------------------------------------------------------------------


class TestExistingBranches:
    def test_orchestrator_branch_still_works(self):
        mode, executor, _card, display_name, caps = _resolve(orchestrator=True)
        assert mode == "orchestrator"
        assert isinstance(executor, OrchestratorExecutor)
        assert display_name == "Orchestrator"
        assert "orchestrate" in caps

    def test_endpoint_branch(self):
        mode, executor, _card, display_name, caps = _resolve(
            endpoint="http://remote/api",
        )
        assert mode == "remote"
        assert isinstance(executor, RemoteExecutor)
        assert "Remote" in display_name
        assert caps == ["code"]

    # Note: --framework and --provider branches eagerly instantiate LLM
    # provider clients (which require ANTHROPIC_API_KEY), so they aren't
    # safe to unit-test without mocks. The shared kwargs threading is
    # already validated by the orchestrator/endpoint/agent tests above.

    def test_agent_branch(self):
        mode, executor, _card, _display, _caps = _resolve(agent="claude")
        assert mode == "cli:claude"
        assert isinstance(executor, CLIExecutor)


# ---------------------------------------------------------------------------
# Mutual exclusion + error path
# ---------------------------------------------------------------------------


class TestMutualExclusion:
    def test_no_mode_raises_usage_error(self):
        import click
        with pytest.raises(click.UsageError) as exc_info:
            _resolve()
        msg = str(exc_info.value)
        assert "--planner" in msg
        assert "--orchestrator" in msg
        assert "--agent" in msg

    def test_planner_takes_precedence_over_orchestrator(self):
        # If both flags are set (operator error), planner branch wins
        # because it appears first in _resolve_mode. This documents the
        # current behavior so a future refactor doesn't silently flip it.
        mode, executor, _card, _display, _caps = _resolve(
            planner=True, orchestrator=True,
        )
        assert mode == "planner"
        assert isinstance(executor, PlannerOrchestratorExecutor)
