from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from krewcli.agents.models import CodeRefResult, FactRefResult


@dataclass(frozen=True)
class TaskRunSpec:
    """Specification for a task to be executed by an agent runtime.

    CRI equivalent: this is what the scheduler/NodeAgent hands to the
    runtime. Contains everything the runtime needs to execute the task.
    """

    task_id: str
    title: str
    description: str = ""
    working_dir: str = "."
    repo_url: str = ""
    branch: str = "main"
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskRunResult:
    """Result of a task execution.

    CRI equivalent: what the runtime returns after running a task.
    """

    success: bool
    summary: str
    files_modified: list[str] = field(default_factory=list)
    facts: list[FactRefResult] = field(default_factory=list)
    code_refs: list[CodeRefResult] = field(default_factory=list)
    blocked_reason: str | None = None
    exit_code: int = 0


@dataclass(frozen=True)
class RuntimeHealth:
    """Health status of an agent runtime."""

    healthy: bool
    message: str = ""
    runtime_name: str = ""


class AgentRuntimeInterface(Protocol):
    """CRI equivalent — abstract interface for running agent workloads.

    Implementations wrap specific agent backends (Claude, Codex, Bub, etc.)
    behind a uniform interface. The NodeAgent/TaskRunner calls run_task()
    without knowing which backend is executing.

    Workload types:
    - Job: one-shot task execution (current behavior)
    - CronJob: recurring tasks (future)
    - Deployment: long-running agent (future)
    """

    async def run_task(self, spec: TaskRunSpec) -> TaskRunResult:
        """Execute a single task. Blocks until completion or failure."""
        ...

    async def health_check(self) -> RuntimeHealth:
        """Report runtime health. Used by PresenceController."""
        ...

    def capabilities(self) -> list[str]:
        """Report what this runtime can do."""
        ...
