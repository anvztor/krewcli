from __future__ import annotations

import pytest

from krewcli.agents.base import AgentDeps, AgentRunResult
from krewcli.agents.models import CodeRefResult, FactRefResult
from krewcli.agents.models import TaskResult
from krewcli.runtime.interface import (
    RuntimeHealth,
    TaskRunResult,
    TaskRunSpec,
)
from krewcli.runtime.job import JobRuntime


def test_task_run_spec_immutable():
    spec = TaskRunSpec(task_id="t1", title="Test")
    assert spec.task_id == "t1"
    assert spec.description == ""
    assert spec.working_dir == "."
    assert spec.context == {}


def test_task_run_result_defaults():
    result = TaskRunResult(success=True, summary="Done")
    assert result.files_modified == []
    assert result.facts == []
    assert result.code_refs == []
    assert result.blocked_reason is None
    assert result.exit_code == 0


def test_task_run_result_with_artifacts():
    result = TaskRunResult(
        success=True,
        summary="Created feature",
        files_modified=["src/foo.py"],
        facts=[FactRefResult(claim="Feature works")],
        code_refs=[CodeRefResult(
            repo_url="git@github.com:test/repo.git",
            branch="main",
            commit_sha="abc123",
            paths=["src/foo.py"],
        )],
        exit_code=0,
    )
    assert len(result.facts) == 1
    assert result.facts[0].claim == "Feature works"
    assert result.code_refs[0].paths == ["src/foo.py"]


def test_runtime_health():
    health = RuntimeHealth(healthy=True, message="ok", runtime_name="job:claude")
    assert health.healthy
    assert health.runtime_name == "job:claude"


def test_job_runtime_capabilities():
    runtime = JobRuntime("claude")
    caps = runtime.capabilities()
    assert "claim" in caps
    assert "milestones" in caps


@pytest.mark.asyncio
async def test_job_runtime_health_check():
    runtime = JobRuntime("claude")
    health = await runtime.health_check()
    assert isinstance(health.healthy, bool)
    assert "claude" in health.message


class MockRuntime:
    """Mock AgentRuntimeInterface for testing."""

    def __init__(self, result: TaskRunResult) -> None:
        self._result = result
        self.last_spec: TaskRunSpec | None = None

    async def run_task(self, spec: TaskRunSpec) -> TaskRunResult:
        self.last_spec = spec
        return self._result

    async def health_check(self) -> RuntimeHealth:
        return RuntimeHealth(healthy=True, runtime_name="mock")

    def capabilities(self) -> list[str]:
        return ["claim", "milestones"]


def test_mock_runtime_satisfies_protocol():
    """Verify MockRuntime satisfies AgentRuntimeInterface structurally."""
    mock = MockRuntime(TaskRunResult(success=True, summary="ok"))
    # Protocol compliance: all required methods exist
    assert hasattr(mock, "run_task")
    assert hasattr(mock, "health_check")
    assert hasattr(mock, "capabilities")


@pytest.mark.asyncio
async def test_mock_runtime_execution():
    expected = TaskRunResult(
        success=True,
        summary="Task completed",
        files_modified=["a.py"],
    )
    mock = MockRuntime(expected)
    spec = TaskRunSpec(task_id="t1", title="Test task", working_dir="/tmp")

    result = await mock.run_task(spec)
    assert result.success
    assert result.summary == "Task completed"
    assert mock.last_spec == spec


@pytest.mark.asyncio
async def test_job_runtime_passes_stringified_context(monkeypatch):
    captured: dict[str, AgentDeps] = {}

    class FakeAgent:
        async def run(self, prompt: str, *, deps: AgentDeps) -> AgentRunResult:
            assert "Complete this task: Test task" in prompt
            captured["deps"] = deps
            return AgentRunResult(output=TaskResult(summary="ok", success=True))

    monkeypatch.setattr("krewcli.runtime.job.get_agent", lambda _name: FakeAgent())

    runtime = JobRuntime("codex")
    result = await runtime.run_task(
        TaskRunSpec(
            task_id="t1",
            title="Test task",
            working_dir="/tmp",
            context={
                "CODEX_HOME": "/tmp/codex-home",
                "attempt": 2,
                "skip": None,
            },
        )
    )

    assert result.success is True
    assert captured["deps"].context == {
        "CODEX_HOME": "/tmp/codex-home",
        "attempt": "2",
    }
