from __future__ import annotations

import logging

from krewcli.agents.base import AgentDeps
from krewcli.agents.registry import get_agent, get_agent_info
from krewcli.runtime.interface import (
    RuntimeHealth,
    TaskRunResult,
    TaskRunSpec,
)

logger = logging.getLogger(__name__)


class JobRuntime:
    """Job workload type — one-shot task execution.

    Wraps a LocalCliAgent behind the AgentRuntimeInterface.
    This is the CRI implementation for the existing agent backends.
    """

    def __init__(self, agent_name: str) -> None:
        self._agent_name = agent_name
        self._info = get_agent_info(agent_name)

    async def run_task(self, spec: TaskRunSpec) -> TaskRunResult:
        agent = get_agent(self._agent_name)
        deps = AgentDeps(
            working_dir=spec.working_dir,
            repo_url=spec.repo_url,
            branch=spec.branch,
            context=_stringify_context(spec.context),
        )

        prompt = (
            f"Complete this task: {spec.title}\n"
            f"Description: {spec.description or 'No description'}\n"
            f"Working directory: {spec.working_dir}\n"
            f"Repository: {spec.repo_url} branch: {spec.branch}"
        )

        # Inject context from tape/CSI if available
        if spec.context:
            context_summary = spec.context.get("summary", "")
            if context_summary:
                prompt += f"\n\nContext from previous work:\n{context_summary}"

        result = await agent.run(prompt, deps=deps)
        task_result = result.output

        return TaskRunResult(
            success=task_result.success,
            summary=task_result.summary,
            files_modified=task_result.files_modified,
            facts=task_result.facts,
            code_refs=task_result.code_refs,
            blocked_reason=task_result.blocked_reason,
            exit_code=0 if task_result.success else 1,
        )

    async def health_check(self) -> RuntimeHealth:
        # For CLI-based agents, health is determined by whether
        # the CLI tool is available on PATH
        import shutil
        cmd = self._agent_name
        if cmd == "claude":
            cmd = "claude"
        elif cmd == "codex":
            cmd = "codex"
        elif cmd == "bub":
            cmd = "bub"

        available = shutil.which(cmd) is not None
        return RuntimeHealth(
            healthy=available,
            message=f"{cmd} CLI {'found' if available else 'not found'} on PATH",
            runtime_name=f"job:{self._agent_name}",
        )

    def capabilities(self) -> list[str]:
        return list(self._info["capabilities"])


def _stringify_context(context: dict[object, object]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in context.items():
        if value is None:
            continue
        out[str(key)] = str(value)
    return out
