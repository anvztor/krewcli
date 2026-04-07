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
async def test_codex_agent_run_uses_command_runner_without_hook_context(monkeypatch):
    calls: list[tuple[tuple[str, ...], str, int]] = []

    async def fake_run_command(
        args: list[str],
        working_dir: str,
        *,
        timeout: int = 30,
    ) -> CommandResult:
        calls.append((tuple(args), working_dir, timeout))
        if args[0] == "codex":
            return CommandResult(0, "Updated the local workspace", "")
        if args[:2] == ["git", "status"]:
            return CommandResult(0, "M  src/app.py\n", "")
        if args[:3] == ["git", "config", "--get"]:
            return CommandResult(0, "git@example.com:org/repo.git\n", "")
        if args[:2] == ["git", "rev-parse"]:
            return CommandResult(0, "abc123\n", "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(base, "_run_command", fake_run_command)

    agent = create_codex_agent()
    result = await agent.run("fix it", deps=AgentDeps(working_dir=".", repo_url="", branch="main"))

    assert result.output.success is True
    assert result.output.summary == "Updated the local workspace"
    assert result.output.files_modified == ["src/app.py"]
    assert result.output.code_refs[0].commit_sha == "abc123"
    assert calls == [
        (("codex", "exec", "--skip-git-repo-check", "--full-auto", "fix it"), ".", 1800),
        (("git", "status", "--short"), ".", 30),
        (("git", "config", "--get", "remote.origin.url"), ".", 30),
        (("git", "rev-parse", "HEAD"), ".", 30),
    ]


@pytest.mark.asyncio
async def test_codex_agent_run_uses_rollout_watcher_with_task_context(monkeypatch, tmp_path):
    """With KREWHUB_TASK_ID bound, the agent takes the rollout-watcher path.

    We intentionally do NOT set CODEX_HOME — isolating codex breaks auth
    and we tail the user's real ~/.codex the same way vibe-island does.
    """
    from pathlib import Path as _Path

    calls: list[tuple[tuple[str, ...], str, int]] = []
    spawn_calls: list[tuple[tuple[str, ...], dict]] = []
    watcher_events: list[str] = []
    home = _Path.home()

    async def fake_run_command(
        args: list[str],
        working_dir: str,
        *,
        timeout: int = 30,
    ) -> CommandResult:
        calls.append((tuple(args), working_dir, timeout))
        if args[:2] == ["git", "status"]:
            return CommandResult(0, "M  src/bridge.py\n", "")
        if args[:3] == ["git", "config", "--get"]:
            return CommandResult(0, "git@example.com:org/repo.git\n", "")
        if args[:2] == ["git", "rev-parse"]:
            return CommandResult(0, "def456\n", "")
        raise AssertionError(f"unexpected command: {args}")

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    class FakeWatcher:
        def __init__(self, *, codex_home: str, env: dict[str, str], session_id_hint: str | None = None):
            assert codex_home == str(home / ".codex")
            assert env == {"KREWHUB_TASK_ID": "task-123"}
            assert session_id_hint is None
            self.latest_rollout_path = tmp_path / "rollout.jsonl"

        async def start(self) -> None:
            watcher_events.append("start")

        async def stop(self) -> None:
            watcher_events.append("stop")

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawn_calls.append((args, kwargs))
        return FakeProcess()

    async def fake_extract_summary_from_rollout(*, rollout_path, fallback_stderr: str, success: bool) -> str:
        assert rollout_path == tmp_path / "rollout.jsonl"
        assert fallback_stderr == ""
        assert success is True
        return "Forwarded rollout summary"

    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(base, "_run_command", fake_run_command)
    monkeypatch.setattr(codex_agent, "CodexRolloutWatcher", FakeWatcher)
    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(codex_agent, "_extract_summary_from_rollout", fake_extract_summary_from_rollout)

    agent = create_codex_agent()
    result = await agent.run(
        "stream it",
        deps=AgentDeps(
            working_dir=".",
            repo_url="",
            branch="main",
            context={"KREWHUB_TASK_ID": "task-123"},
        ),
    )

    assert result.output.success is True
    assert result.output.summary == "Forwarded rollout summary"
    assert result.output.files_modified == ["src/bridge.py"]
    assert result.output.code_refs[0].commit_sha == "def456"
    assert spawn_calls[0][0] == (
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--full-auto",
        "stream it",
    )
    assert spawn_calls[0][1]["cwd"] == "."
    # KREWHUB_TASK_ID flows through; CODEX_HOME is deliberately stripped.
    assert spawn_calls[0][1]["env"]["KREWHUB_TASK_ID"] == "task-123"
    assert "CODEX_HOME" not in spawn_calls[0][1]["env"]
    assert watcher_events == ["start", "stop"]
    assert calls == [
        (("git", "status", "--short"), ".", 30),
        (("git", "config", "--get", "remote.origin.url"), ".", 30),
        (("git", "rev-parse", "HEAD"), ".", 30),
    ]


@pytest.mark.asyncio
async def test_codex_agent_strips_codex_home_from_spawn_env(monkeypatch, tmp_path):
    """Even if CODEX_HOME leaks into the process env, we must strip it
    before spawning codex so auth (which lives in the global ~/.codex)
    still works."""
    from pathlib import Path as _Path

    spawn_calls: list[tuple[tuple[str, ...], dict]] = []
    home = _Path.home()

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    class FakeWatcher:
        def __init__(self, *, codex_home: str, env: dict[str, str], session_id_hint: str | None = None):
            # Watcher is pointed at the user's real home, never the leaked env var.
            assert codex_home == str(home / ".codex")
            assert "CODEX_HOME" not in env
            self.latest_rollout_path = tmp_path / "rollout.jsonl"

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawn_calls.append((args, kwargs))
        return FakeProcess()

    async def fake_extract_summary_from_rollout(*, rollout_path, fallback_stderr: str, success: bool) -> str:
        del rollout_path, fallback_stderr
        assert success is True
        return "ok"

    async def fake_run_command(
        args: list[str],
        working_dir: str,
        *,
        timeout: int = 30,
    ) -> CommandResult:
        del working_dir, timeout
        if args[:2] == ["git", "status"]:
            return CommandResult(0, "", "")
        if args[:3] == ["git", "config", "--get"]:
            return CommandResult(0, "", "")
        if args[:2] == ["git", "rev-parse"]:
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setenv("CODEX_HOME", "/some/leaked/path")
    monkeypatch.setattr(base, "_run_command", fake_run_command)
    monkeypatch.setattr(codex_agent, "CodexRolloutWatcher", FakeWatcher)
    monkeypatch.setattr(codex_agent.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(codex_agent, "_extract_summary_from_rollout", fake_extract_summary_from_rollout)

    agent = create_codex_agent()
    await agent.run(
        "stream it",
        deps=AgentDeps(
            working_dir=".",
            repo_url="",
            branch="main",
            context={"KREWHUB_TASK_ID": "task-strip"},
        ),
    )

    assert "CODEX_HOME" not in spawn_calls[0][1]["env"]


@pytest.mark.asyncio
async def test_codex_agent_run_handles_missing_git(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def fake_run_command(
        args: list[str],
        working_dir: str,
        *,
        timeout: int = 30,
    ) -> CommandResult:
        del working_dir, timeout
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
    assert calls == [
        ("codex", "exec", "--skip-git-repo-check", "--full-auto", "say hi"),
        ("git", "status", "--short"),
        ("git", "config", "--get", "remote.origin.url"),
        ("git", "rev-parse", "HEAD"),
    ]
