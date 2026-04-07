from __future__ import annotations

import pytest

from krewcli.agents import base
from krewcli.agents import codex_agent
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
async def test_codex_agent_run_collects_rollout_summary_and_git_metadata(monkeypatch):
    calls: list[tuple[tuple[str, ...], str, int]] = []
    spawn_calls: list[tuple[tuple[str, ...], dict]] = []
    watcher_events: list[str] = []

    async def fake_run_command(args: list[str], working_dir: str, *, timeout: int = 30) -> CommandResult:
        calls.append((tuple(args), working_dir, timeout))
        if args[:2] == ["git", "status"]:
            return CommandResult(0, "M  src/app.py\n", "")
        if args[:3] == ["git", "config", "--get"]:
            return CommandResult(0, "git@example.com:org/repo.git\n", "")
        if args[:2] == ["git", "rev-parse"]:
            return CommandResult(0, "abc123\n", "")
        raise AssertionError(f"unexpected command: {args}")

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    class FakeWatcher:
        latest_rollout_path = None

        def __init__(self, *, codex_home: str, env: dict[str, str], session_id_hint: str | None = None):
            assert codex_home
            assert "CODEX_HOME" in env
            assert session_id_hint is None

        async def start(self) -> None:
            watcher_events.append("start")

        async def stop(self) -> None:
            watcher_events.append("stop")

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawn_calls.append((args, kwargs))
        return FakeProcess()

    async def fake_extract_summary_from_rollout(*, rollout_path, fallback_stderr: str, success: bool) -> str:
        assert rollout_path is None
        assert fallback_stderr == ""
        assert success is True
        return "Updated the local workspace"

    monkeypatch.setattr(base, "_run_command", fake_run_command)
    monkeypatch.setattr(codex_agent, "CodexRolloutWatcher", FakeWatcher)
    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(codex_agent, "_extract_summary_from_rollout", fake_extract_summary_from_rollout)

    agent = create_codex_agent()
    result = await agent.run(
        "fix it",
        deps=AgentDeps(
            working_dir=".",
            repo_url="",
            branch="main",
            context={
                "CODEX_HOME": "/tmp/krew-verify/.codex",
                "KREWHUB_TASK_ID": "task_test",
            },
        ),
    )

    assert result.output.success is True
    assert result.output.summary == "Updated the local workspace"
    assert result.output.files_modified == ["src/app.py"]
    assert result.output.code_refs[0].commit_sha == "abc123"
    assert spawn_calls[0][0] == (
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--full-auto",
        "fix it",
    )
    assert spawn_calls[0][1]["cwd"] == "."
    assert watcher_events == ["start", "stop"]
    assert calls == [
        (("git", "status", "--short"), ".", 30),
        (("git", "config", "--get", "remote.origin.url"), ".", 30),
        (("git", "rev-parse", "HEAD"), ".", 30),
    ]


@pytest.mark.asyncio
async def test_codex_agent_run_handles_missing_git(monkeypatch):
    calls: list[tuple[str, ...]] = []
    spawn_calls: list[tuple[str, ...]] = []
    watcher_events: list[str] = []

    async def fake_run_command(
        args: list[str],
        working_dir: str,
        *,
        timeout: int = 30,
    ) -> CommandResult:
        calls.append(tuple(args))
        raise FileNotFoundError(args[0])

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    class FakeWatcher:
        latest_rollout_path = None

        def __init__(self, **_: object):
            pass

        async def start(self) -> None:
            watcher_events.append("start")

        async def stop(self) -> None:
            watcher_events.append("stop")

    async def fake_create_subprocess_exec(*args, **kwargs):
        del kwargs
        spawn_calls.append(tuple(args))
        return FakeProcess()

    async def fake_extract_summary_from_rollout(*, rollout_path, fallback_stderr: str, success: bool) -> str:
        del rollout_path, fallback_stderr
        assert success is True
        return "Hi from Codex"

    monkeypatch.setattr(base, "_run_command", fake_run_command)
    monkeypatch.setattr(codex_agent, "CodexRolloutWatcher", FakeWatcher)
    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(codex_agent, "_extract_summary_from_rollout", fake_extract_summary_from_rollout)

    agent = create_codex_agent()
    result = await agent.run(
        "say hi",
        deps=AgentDeps(
            working_dir=".",
            repo_url="",
            branch="main",
            context={
                "CODEX_HOME": "/tmp/krew-verify/.codex",
                "KREWHUB_TASK_ID": "task_test",
            },
        ),
    )

    assert result.output.success is True
    assert result.output.files_modified == []
    assert result.output.code_refs == []
    assert result.output.summary == "Hi from Codex"
    assert spawn_calls == [
        (
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--full-auto",
            "say hi",
        )
    ]
    assert watcher_events == ["start", "stop"]
    assert calls == [
        ("git", "status", "--short"),
        ("git", "config", "--get", "remote.origin.url"),
        ("git", "rev-parse", "HEAD"),
    ]
