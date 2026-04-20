"""Unit tests for krewcli.agents.base — dataclasses and helper functions.

Covers HarnessConfig, AgentDeps, AgentRunResult, and shared output
summarization. Local CLI behavior is imported through the refactored
``krewcli.agents.local_cli`` boundary.
"""

from __future__ import annotations

import asyncio

import pytest

from krewcli.agents import base
from krewcli.agents.base import (
    AgentDeps,
    AgentRunResult,
    HarnessConfig,
    _summarize_output,
)
from krewcli.agents.local_cli import CommandResult, LocalCliAgent


class TestHarnessConfig:
    def test_defaults(self):
        cfg = HarnessConfig()
        assert cfg.timeout == 300
        assert cfg.max_retries == 0
        assert cfg.allowed_tools == ()

    def test_custom_values(self):
        cfg = HarnessConfig(timeout=60, max_retries=3, allowed_tools=("bash", "read"))
        assert cfg.timeout == 60
        assert cfg.max_retries == 3
        assert cfg.allowed_tools == ("bash", "read")


class TestAgentDeps:
    def test_defaults(self):
        deps = AgentDeps(working_dir="/tmp", repo_url="", branch="main")
        assert deps.system_prompt == ""
        assert deps.harness is None
        assert deps.hooks == {}
        assert deps.context == {}
        assert deps.event_sink is None

    def test_with_harness(self):
        harness = HarnessConfig(timeout=120)
        deps = AgentDeps(
            working_dir="/work",
            repo_url="git@example.com:repo.git",
            branch="feat",
            harness=harness,
        )
        assert deps.harness.timeout == 120


class TestCommandResult:
    def test_fields(self):
        r = CommandResult(returncode=0, stdout="ok\n", stderr="")
        assert r.returncode == 0
        assert r.stdout == "ok\n"
        assert r.stderr == ""

    def test_nonzero_return(self):
        r = CommandResult(returncode=1, stdout="", stderr="error")
        assert r.returncode == 1


class TestAgentRunResult:
    def test_wraps_task_result(self):
        from krewcli.agents.models import TaskResult
        tr = TaskResult(summary="done", success=True)
        arr = AgentRunResult(output=tr)
        assert arr.output.success is True
        assert arr.output.summary == "done"


class TestSummarizeOutput:
    def test_normalizes_whitespace(self):
        result = _summarize_output("  hello\n  world  ", success=True, name="Test")
        assert result == "hello world"

    def test_empty_output_success(self):
        result = _summarize_output("", success=True, name="Claude")
        assert result == "Claude completed successfully"

    def test_empty_output_failure(self):
        result = _summarize_output("", success=False, name="Codex")
        assert result == "Codex failed without producing output"

    def test_whitespace_only_is_empty(self):
        result = _summarize_output("   \n\t  ", success=True, name="Bub")
        assert result == "Bub completed successfully"

    def test_preserves_content_on_failure(self):
        result = _summarize_output("error: missing file", success=False, name="Agent")
        assert result == "error: missing file"


class TestLocalCliAgentNoSink:
    """LocalCliAgent edge cases when event_sink is None (legacy path)."""

    @pytest.mark.asyncio
    async def test_file_not_found_returns_blocked(self, monkeypatch):
        async def _raise_fnf(args, working_dir, *, timeout=30):
            raise FileNotFoundError("fake-cli")

        monkeypatch.setattr(base, "_run_command", _raise_fnf)

        agent = LocalCliAgent(name="fake", command_builder=lambda p: ["fake-cli", p])
        result = await agent.run(
            "do something",
            deps=AgentDeps(working_dir="/tmp", repo_url="", branch="main"),
        )
        assert result.output.success is False
        assert "not installed" in result.output.summary
        assert result.output.blocked_reason is not None

    @pytest.mark.asyncio
    async def test_timeout_returns_blocked(self, monkeypatch):
        async def _raise_timeout(args, working_dir, *, timeout=30):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(base, "_run_command", _raise_timeout)

        agent = LocalCliAgent(name="slow", command_builder=lambda p: ["slow-cli", p])
        result = await agent.run(
            "hang forever",
            deps=AgentDeps(working_dir="/tmp", repo_url="", branch="main"),
        )
        assert result.output.success is False
        assert "timed out" in result.output.summary

    @pytest.mark.asyncio
    async def test_uses_harness_timeout(self, monkeypatch):
        captured_timeout = []

        async def _capture_run(args, working_dir, *, timeout=30):
            captured_timeout.append(timeout)
            return CommandResult(returncode=0, stdout="ok", stderr="")

        async def _fake_git(args, working_dir, *, allow_empty=False):
            return ""

        monkeypatch.setattr(base, "_run_command", _capture_run)
        monkeypatch.setattr(base, "_read_git_value", _fake_git)
        async def _fake_list(_wd):
            return []

        monkeypatch.setattr(base, "_list_changed_files", _fake_list)

        agent = LocalCliAgent(name="test", command_builder=lambda p: ["test", p])
        harness = HarnessConfig(timeout=42)
        await agent.run(
            "go",
            deps=AgentDeps(working_dir="/tmp", repo_url="", branch="main", harness=harness),
        )
        assert captured_timeout[0] == 42

    @pytest.mark.asyncio
    async def test_default_timeout_without_harness(self, monkeypatch):
        captured_timeout = []

        async def _capture_run(args, working_dir, *, timeout=30):
            captured_timeout.append(timeout)
            return CommandResult(returncode=0, stdout="done", stderr="")

        async def _fake_git(args, working_dir, *, allow_empty=False):
            return ""

        monkeypatch.setattr(base, "_run_command", _capture_run)
        monkeypatch.setattr(base, "_read_git_value", _fake_git)
        async def _fake_list(_wd):
            return []

        monkeypatch.setattr(base, "_list_changed_files", _fake_list)

        agent = LocalCliAgent(name="test", command_builder=lambda p: ["test", p])
        await agent.run(
            "go",
            deps=AgentDeps(working_dir="/tmp", repo_url="", branch="main"),
        )
        assert captured_timeout[0] == base._DEFAULT_LOCAL_TIMEOUT
