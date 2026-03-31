from __future__ import annotations

import pytest

from krewcli.agents import base
from krewcli.agents.base import AgentDeps, CommandResult
from krewcli.agents.bub_agent import create_bub_agent
from krewcli.agents.claude_agent import create_claude_agent
from krewcli.agents.codex_agent import create_codex_agent


def test_local_agent_wrappers_do_not_require_provider_keys():
    deps = AgentDeps(working_dir=".", repo_url="", branch="main")

    for factory in (create_codex_agent, create_claude_agent, create_bub_agent):
        agent = factory()
        assert agent is not None
        assert callable(agent.run)
        assert deps.branch == "main"


@pytest.mark.asyncio
async def test_local_agent_run_uses_async_command_runner(monkeypatch):
    calls: list[tuple[tuple[str, ...], str, int]] = []

    async def fake_run_command(args: list[str], working_dir: str, *, timeout: int = 30) -> CommandResult:
        calls.append((tuple(args), working_dir, timeout))
        if args[:2] == ["git", "status"]:
            return CommandResult(0, "M  src/app.py\n", "")
        if args[:3] == ["git", "config", "--get"]:
            return CommandResult(0, "git@example.com:org/repo.git\n", "")
        if args[:2] == ["git", "rev-parse"]:
            return CommandResult(0, "abc123\n", "")
        return CommandResult(0, "Updated the local workspace", "")

    monkeypatch.setattr(base, "_run_command", fake_run_command)

    agent = create_codex_agent()
    result = await agent.run("fix it", deps=AgentDeps(working_dir=".", repo_url="", branch="main"))

    assert result.output.success is True
    assert result.output.files_modified == ["src/app.py"]
    assert result.output.code_refs[0].commit_sha == "abc123"
    assert calls[0][0] == (
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--full-auto",
        "fix it",
    )


@pytest.mark.asyncio
async def test_local_agent_run_handles_missing_git(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_run_command(
        args: list[str],
        working_dir: str,
        *,
        timeout: int = 30,
    ) -> CommandResult:
        calls.append(tuple(args))
        if args[0] == "codex":
            return CommandResult(0, "Hi from Codex", "")
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(base, "_run_command", fake_run_command)

    agent = create_codex_agent()
    result = await agent.run(
        "say hi",
        deps=AgentDeps(working_dir=".", repo_url="", branch="main"),
    )

    assert result.output.success is True
    assert result.output.files_modified == []
    assert result.output.code_refs == []
    assert result.output.summary == "Hi from Codex"
    assert calls[0][0] == "codex"
