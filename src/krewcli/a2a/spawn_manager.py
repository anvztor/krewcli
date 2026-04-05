"""Spawn manager — launches and tracks CLI agent subprocesses.

The gateway executor delegates to SpawnManager to run CLI agents
(claude, codex, bub) as on-demand subprocesses. Each task gets its
own process that starts, executes, and exits.

Reports results back to krewhub via the A2A callback endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field

from krewcli.agents.base import AgentDeps, AgentRunResult
from krewcli.agents.models import TaskResult
from krewcli.agents.registry import get_agent

logger = logging.getLogger(__name__)


@dataclass
class SpawnSession:
    """Tracks a running CLI agent subprocess."""
    task_id: str
    agent_name: str
    agent_id: str
    task: asyncio.Task | None = None


@dataclass
class SpawnResult:
    """Result from a spawned CLI session."""
    task_id: str
    agent_id: str
    success: bool
    summary: str = ""
    blocked_reason: str | None = None
    files_modified: list[str] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    code_refs: list[dict] = field(default_factory=list)


class SpawnManager:
    """Manages CLI agent subprocess lifecycle.

    - Tracks running sessions per agent type
    - Enforces max_concurrent per agent
    - Spawns on-demand, collects results
    - Reports back via callback
    - Supports per-recipe working directories via recipe_contexts
    """

    def __init__(
        self,
        working_dir: str,
        repo_url: str = "",
        branch: str = "main",
        callback_url: str = "",
        api_key: str = "",
        recipe_contexts: dict[str, dict] | None = None,
    ) -> None:
        self._working_dir = working_dir
        self._repo_url = repo_url
        self._branch = branch
        self._callback_url = callback_url
        self._api_key = api_key
        self._recipe_contexts = recipe_contexts or {}
        self._sessions: dict[str, SpawnSession] = {}

    @property
    def running_count(self) -> int:
        return len(self._sessions)

    def running_count_for(self, agent_name: str) -> int:
        return sum(
            1 for s in self._sessions.values()
            if s.agent_name == agent_name
        )

    def is_available(self, agent_name: str) -> bool:
        """Check if the CLI binary for this agent is on PATH."""
        return shutil.which(agent_name) is not None

    def resolve_recipe_context(self, recipe_name: str) -> dict:
        """Resolve per-recipe working_dir/repo_url/branch.

        Falls back to constructor defaults when recipe_name is unknown.
        """
        ctx = self._recipe_contexts.get(recipe_name, {})
        return {
            "working_dir": ctx.get("working_dir", self._working_dir),
            "repo_url": ctx.get("repo_url", self._repo_url),
            "branch": ctx.get("branch", self._branch),
        }

    async def spawn(
        self,
        agent_name: str,
        agent_id: str,
        task_id: str,
        prompt: str,
        working_dir: str | None = None,
        repo_url: str | None = None,
        branch: str | None = None,
    ) -> bool:
        """Spawn a CLI agent to execute a task. Returns True if started."""
        if task_id in self._sessions:
            logger.warning("SpawnManager: task %s already running", task_id)
            return False

        session = SpawnSession(
            task_id=task_id,
            agent_name=agent_name,
            agent_id=agent_id,
        )
        self._sessions[task_id] = session

        session.task = asyncio.create_task(
            self._run_and_report(
                session, prompt,
                working_dir=working_dir or self._working_dir,
                repo_url=repo_url or self._repo_url,
                branch=branch or self._branch,
            ),
            name=f"spawn:{agent_name}:{task_id}",
        )
        logger.info(
            "SpawnManager: spawned %s for task %s", agent_name, task_id,
        )
        return True

    async def _run_and_report(
        self,
        session: SpawnSession,
        prompt: str,
        working_dir: str = "",
        repo_url: str = "",
        branch: str = "main",
    ) -> None:
        """Execute CLI agent and report results to krewhub callback."""
        result = await self._execute(
            session.agent_name, prompt,
            working_dir=working_dir, repo_url=repo_url, branch=branch,
        )
        result.task_id = session.task_id
        result.agent_id = session.agent_id

        self._sessions.pop(session.task_id, None)

        if self._callback_url:
            await self._report_callback(result)

        logger.info(
            "SpawnManager: task %s finished (success=%s)",
            session.task_id, result.success,
        )

    async def _execute(
        self,
        agent_name: str,
        prompt: str,
        working_dir: str = "",
        repo_url: str = "",
        branch: str = "main",
    ) -> SpawnResult:
        """Run the agent CLI and collect results."""
        try:
            agent = get_agent(agent_name)
            deps = AgentDeps(
                working_dir=working_dir or self._working_dir,
                repo_url=repo_url or self._repo_url,
                branch=branch or self._branch,
            )
            run_result: AgentRunResult = await agent.run(prompt, deps=deps)
            task_result: TaskResult = run_result.output

            return SpawnResult(
                task_id="",
                agent_id="",
                success=task_result.success,
                summary=task_result.summary,
                blocked_reason=task_result.blocked_reason,
                files_modified=task_result.files_modified,
                code_refs=[
                    {
                        "repo_url": cr.repo_url,
                        "branch": cr.branch,
                        "commit_sha": cr.commit_sha,
                        "paths": cr.paths,
                    }
                    for cr in task_result.code_refs
                ],
            )
        except Exception as exc:
            logger.exception("SpawnManager: execution failed for %s", agent_name)
            return SpawnResult(
                task_id="",
                agent_id="",
                success=False,
                summary=f"Agent {agent_name} failed: {exc}",
                blocked_reason=f"Agent {agent_name} failed: {exc}",
            )

    async def _report_callback(self, result: SpawnResult) -> None:
        """POST result to krewhub's A2A callback endpoint."""
        import httpx

        payload = {
            "task_id": result.task_id,
            "agent_id": result.agent_id,
            "success": result.success,
            "summary": result.summary,
            "blocked_reason": result.blocked_reason,
            "files_modified": result.files_modified,
            "facts": result.facts,
            "code_refs": result.code_refs,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    self._callback_url,
                    json=payload,
                    headers={"X-API-Key": self._api_key},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "SpawnManager: callback failed for task %s (status=%d): %s",
                        result.task_id, resp.status_code, resp.text,
                    )
        except Exception:
            logger.exception(
                "SpawnManager: callback unreachable for task %s", result.task_id,
            )

    async def cancel(self, task_id: str) -> bool:
        """Cancel a running spawn session."""
        session = self._sessions.pop(task_id, None)
        if session is None:
            return False
        if session.task is not None and not session.task.done():
            session.task.cancel()
        return True

    async def shutdown(self) -> None:
        """Cancel all running sessions."""
        for task_id in list(self._sessions):
            await self.cancel(task_id)
