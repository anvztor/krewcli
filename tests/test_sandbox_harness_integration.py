"""Integration tests: sandbox validation within the harness pipeline.

Verifies that the Harness correctly gates task execution on sandbox
checks — rejecting tasks with credential leaks (pre-execution) and
flagging secret exfiltration or file boundary escapes (post-execution).

These tests use in-memory fakes for the backend, session, and execenv
so no real subprocesses or krewhub calls are needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from krewcli.backend.protocol import (
    BackendMessage,
    BackendResult,
    BackendSession,
)
from krewcli.daemon.harness import Harness, HarnessResult
from krewcli.daemon.execenv import ExecutionEnvironment
from krewcli.daemon.session import Session


# ── Fake helpers ────────────────────────────────────────────────────


class _FakeClient:
    """Minimal KrewHubClient stand-in."""

    def __init__(self):
        self.status_updates: list[tuple[str, str]] = []
        self.cancel_status = False

    async def update_task_status(self, task_id, status, blocked_reason=None):
        self.status_updates.append((task_id, status))
        return {"id": task_id, "status": status}

    async def post_events_batch(self, task_id, events):
        return [{"id": f"evt_{i}"} for i in range(len(events))]

    async def post_task_completion(self, task_id, session_id, work_dir, artifacts=None):
        return {"id": task_id}

    async def post_task_usage(self, task_id, input_tokens, output_tokens, **kwargs):
        return {}

    async def poll_cancel_status(self, task_id):
        return self.cancel_status


def _make_backend_session(
    messages: list[BackendMessage],
    result: BackendResult,
) -> BackendSession:
    """Build a BackendSession from predetermined messages and result."""
    q: asyncio.Queue[BackendMessage | None] = asyncio.Queue()
    for m in messages:
        q.put_nowait(m)
    q.put_nowait(None)

    fut: asyncio.Future[BackendResult] = asyncio.get_event_loop().create_future()
    fut.set_result(result)
    return BackendSession(q, fut)


class _FakeBackend:
    """Backend that returns predetermined results."""

    name = "fake-backend"

    def __init__(
        self,
        messages: list[BackendMessage] | None = None,
        result: BackendResult | None = None,
    ):
        self._messages = messages or []
        self._result = result or BackendResult(
            success=True,
            summary="Done",
            files_modified=[],
        )

    async def execute(self, prompt, working_dir, *, env=None):
        return _make_backend_session(self._messages, self._result)

    async def health(self):
        return True


class _FakeExecEnv:
    """ExecutionEnvironment that returns a known workdir and env."""

    def __init__(self, workdir: str, env: dict[str, str] | None = None):
        self._workdir = workdir
        self._env = env or {}
        self.teardown_called = False

    async def setup(self, **kwargs) -> str:
        return self._workdir

    async def teardown(self) -> None:
        self.teardown_called = True

    def build_env(self, cookbook_id: str = "", krewhub_url: str = "", session_token: str = "", extra=None) -> dict[str, str]:
        return dict(self._env)


# ── Tests ───────────────────────────────────────────────────────────


class TestHarnessSandboxPreExecution:
    """Sandbox validation before task execution."""

    @pytest.mark.asyncio
    async def test_pre_check_rejects_secret_in_env(self, tmp_path: Path):
        """Harness should abort if env contains known secrets."""
        workdir = tmp_path / "project"
        workdir.mkdir()

        client = _FakeClient()
        harness = Harness(client)

        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI"},
        )
        backend = _FakeBackend()
        session = Session(client, "task-1", "agent-1", flush_interval=0.05)

        result = await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="do work",
            task_id="task-1",
        )

        assert not result.success
        assert "Sandbox validation failed" in result.summary
        assert execenv.teardown_called

    @pytest.mark.asyncio
    async def test_pre_check_rejects_nonexistent_workdir(self):
        """Harness should abort if workdir doesn't exist."""
        client = _FakeClient()
        harness = Harness(client)

        execenv = _FakeExecEnv(
            workdir="/nonexistent/path/xyz",
            env={"KREWHUB_TASK_ID": "task-2"},
        )
        backend = _FakeBackend()
        session = Session(client, "task-2", "agent-1", flush_interval=0.05)

        result = await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="work",
            task_id="task-2",
        )

        assert not result.success
        assert "Sandbox validation failed" in result.summary

    @pytest.mark.asyncio
    async def test_pre_check_passes_clean_env(self, tmp_path: Path):
        """Harness should proceed when env is clean."""
        workdir = tmp_path / "project"
        workdir.mkdir()

        client = _FakeClient()
        harness = Harness(client)

        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"KREWHUB_TASK_ID": "task-3", "PATH": "/usr/bin"},
        )
        backend = _FakeBackend()
        session = Session(client, "task-3", "agent-1", flush_interval=0.05)

        result = await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="work",
            task_id="task-3",
        )

        assert result.success
        await session.flush()


class TestHarnessSandboxPostExecution:
    """Sandbox validation after task execution."""

    @pytest.mark.asyncio
    async def test_post_check_flags_secret_in_output(self, tmp_path: Path):
        """Output containing secrets should cause harness to fail the task."""
        workdir = tmp_path / "project"
        workdir.mkdir()

        client = _FakeClient()
        harness = Harness(client)

        backend_result = BackendResult(
            success=True,
            summary="Deployed with key sk-proj-abcdef1234567890",
            files_modified=[],
        )
        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"KREWHUB_TASK_ID": "task-4"},
        )
        backend = _FakeBackend(result=backend_result)
        session = Session(client, "task-4", "agent-1", flush_interval=0.05)

        result = await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="deploy",
            task_id="task-4",
        )

        assert not result.success
        assert "Sandbox post-check failed" in result.summary
        await session.flush()

    @pytest.mark.asyncio
    async def test_post_check_flags_file_outside_sandbox(self, tmp_path: Path):
        """Files modified outside sandbox should cause failure."""
        workdir = tmp_path / "project"
        workdir.mkdir()

        client = _FakeClient()
        harness = Harness(client)

        backend_result = BackendResult(
            success=True,
            summary="Done",
            files_modified=["/etc/passwd"],
        )
        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"KREWHUB_TASK_ID": "task-5"},
        )
        backend = _FakeBackend(result=backend_result)
        session = Session(client, "task-5", "agent-1", flush_interval=0.05)

        result = await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="edit",
            task_id="task-5",
        )

        assert not result.success
        assert "Sandbox post-check failed" in result.summary
        await session.flush()

    @pytest.mark.asyncio
    async def test_post_check_passes_clean_result(self, tmp_path: Path):
        """Clean output + files within sandbox should pass."""
        workdir = tmp_path / "project"
        workdir.mkdir()
        (workdir / "src").mkdir()

        client = _FakeClient()
        harness = Harness(client)

        backend_result = BackendResult(
            success=True,
            summary="All tests pass",
            files_modified=[str(workdir / "src" / "main.py")],
        )
        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"KREWHUB_TASK_ID": "task-6"},
        )
        backend = _FakeBackend(result=backend_result)
        session = Session(client, "task-6", "agent-1", flush_interval=0.05)

        result = await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="test",
            task_id="task-6",
        )

        assert result.success
        assert result.summary == "All tests pass"
        await session.flush()

    @pytest.mark.asyncio
    async def test_post_check_skipped_on_backend_failure(self, tmp_path: Path):
        """Post-execution checks only run when backend reports success."""
        workdir = tmp_path / "project"
        workdir.mkdir()

        client = _FakeClient()
        harness = Harness(client)

        backend_result = BackendResult(
            success=False,
            summary="Compilation error with sk-proj-abcdef1234567890 in log",
            files_modified=["/etc/passwd"],
        )
        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"KREWHUB_TASK_ID": "task-7"},
        )
        backend = _FakeBackend(result=backend_result)
        session = Session(client, "task-7", "agent-1", flush_interval=0.05)

        result = await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="compile",
            task_id="task-7",
        )

        # Should fail from backend, not from sandbox post-check
        assert not result.success
        assert "Sandbox post-check" not in result.summary
        await session.flush()


class TestHarnessTeardownOnFailure:
    """Harness must always teardown even when sandbox validation fails."""

    @pytest.mark.asyncio
    async def test_teardown_called_on_pre_check_failure(self, tmp_path: Path):
        workdir = tmp_path / "project"
        workdir.mkdir()

        client = _FakeClient()
        harness = Harness(client)

        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"GITHUB_TOKEN": "ghp_" + "x" * 36},
        )
        backend = _FakeBackend()
        session = Session(client, "task-8", "agent-1", flush_interval=0.05)

        await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="work",
            task_id="task-8",
        )

        assert execenv.teardown_called

    @pytest.mark.asyncio
    async def test_teardown_called_on_success(self, tmp_path: Path):
        workdir = tmp_path / "project"
        workdir.mkdir()

        client = _FakeClient()
        harness = Harness(client)

        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"KREWHUB_TASK_ID": "task-9"},
        )
        backend = _FakeBackend()
        session = Session(client, "task-9", "agent-1", flush_interval=0.05)

        await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="work",
            task_id="task-9",
        )

        assert execenv.teardown_called
        await session.flush()


class TestHarnessStatusUpdates:
    """Harness sets correct task statuses based on sandbox outcome."""

    @pytest.mark.asyncio
    async def test_blocked_status_on_pre_check_failure(self, tmp_path: Path):
        workdir = tmp_path / "project"
        workdir.mkdir()

        client = _FakeClient()
        harness = Harness(client)

        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"ANTHROPIC_API_KEY": "sk-ant-secret"},
        )
        backend = _FakeBackend()
        session = Session(client, "task-10", "agent-1", flush_interval=0.05)

        result = await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="work",
            task_id="task-10",
        )

        assert not result.success
        # Pre-check failure returns early before status updates
        # so no "working" status should be set
        working_updates = [
            (tid, s) for tid, s in client.status_updates if s == "working"
        ]
        assert len(working_updates) == 0

    @pytest.mark.asyncio
    async def test_blocked_status_on_post_check_critical(self, tmp_path: Path):
        workdir = tmp_path / "project"
        workdir.mkdir()

        client = _FakeClient()
        harness = Harness(client)

        backend_result = BackendResult(
            success=True,
            summary="Output: ghp_" + "a" * 36,
            files_modified=[],
        )
        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"KREWHUB_TASK_ID": "task-11"},
        )
        backend = _FakeBackend(result=backend_result)
        session = Session(client, "task-11", "agent-1", flush_interval=0.05)

        result = await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="work",
            task_id="task-11",
        )

        assert not result.success
        # Should transition to blocked
        blocked_updates = [
            (tid, s) for tid, s in client.status_updates if s == "blocked"
        ]
        assert len(blocked_updates) == 1
        await session.flush()

    @pytest.mark.asyncio
    async def test_done_status_on_clean_execution(self, tmp_path: Path):
        workdir = tmp_path / "project"
        workdir.mkdir()

        client = _FakeClient()
        harness = Harness(client)

        execenv = _FakeExecEnv(
            workdir=str(workdir),
            env={"KREWHUB_TASK_ID": "task-12"},
        )
        backend = _FakeBackend()
        session = Session(client, "task-12", "agent-1", flush_interval=0.05)

        result = await harness.execute(
            backend=backend,
            session=session,
            execenv=execenv,
            prompt="work",
            task_id="task-12",
        )

        assert result.success
        done_updates = [
            (tid, s) for tid, s in client.status_updates if s == "done"
        ]
        assert len(done_updates) == 1
        await session.flush()
