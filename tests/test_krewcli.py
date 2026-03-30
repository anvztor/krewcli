from __future__ import annotations

import pytest
from click.testing import CliRunner

from krewcli.agents.models import TaskResult, FactRefResult, CodeRefResult
from krewcli.agents.registry import AGENT_REGISTRY, get_agent_info
from krewcli.cli import main
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.workflow.digest_builder import DigestBuilder


def test_agent_registry_has_all_agents():
    assert "codex" in AGENT_REGISTRY
    assert "claude" in AGENT_REGISTRY
    assert "bub" in AGENT_REGISTRY


def test_get_agent_info():
    info = get_agent_info("codex")
    assert info["display_name"] == "Codex Agent"
    assert "claim" in info["capabilities"]


def test_get_agent_info_unknown():
    with pytest.raises(ValueError, match="Unknown agent"):
        get_agent_info("nonexistent")


def test_task_result_model():
    result = TaskResult(
        summary="Added heartbeat endpoint",
        files_modified=["server/heartbeat.py"],
        facts=[
            FactRefResult(claim="Heartbeat < 30s = online", confidence=0.95),
        ],
        code_refs=[
            CodeRefResult(
                repo_url="git@github.com:org/repo.git",
                branch="feat/heartbeat",
                commit_sha="abc123",
                paths=["server/heartbeat.py"],
            ),
        ],
        success=True,
    )
    assert result.success
    assert len(result.facts) == 1
    assert result.facts[0].claim == "Heartbeat < 30s = online"


def test_task_result_blocked():
    result = TaskResult(
        summary="Could not complete",
        success=False,
        blocked_reason="Missing dependency on auth module",
    )
    assert not result.success
    assert result.blocked_reason is not None


def test_cli_status_command():
    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0
    assert "codex" in result.output
    assert "claude" in result.output
    assert "bub" in result.output


def test_digest_builder_add_and_clear():
    client = KrewHubClient("http://fake:1234", "key")
    builder = DigestBuilder(client=client, agent_id="test_agent")

    result_a = TaskResult(
        summary="Task A done",
        facts=[FactRefResult(claim="Fact 1")],
    )
    result_b = TaskResult(
        summary="Task B done",
        code_refs=[
            CodeRefResult(
                repo_url="git@github.com:org/repo.git",
                branch="main",
                commit_sha="def456",
                paths=["src/b.py"],
            )
        ],
    )

    builder.add_result("task_1", result_a)
    builder.add_result("task_2", result_b)
    assert len(builder._results) == 2

    builder.clear()
    assert len(builder._results) == 0


def test_krewhub_client_instantiation():
    client = KrewHubClient("http://127.0.0.1:8420", "test-key")
    assert client._client.base_url == "http://127.0.0.1:8420"
    assert client._client.headers["x-api-key"] == "test-key"
