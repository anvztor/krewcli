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
from krewcli.hooks.scoped_config import build_hook_env
from krewcli.hooks.types import HookWiring
from krewcli.hooks.writers import write_for as write_hooks_for

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
    full_output: str = ""
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
        krewhub_url: str = "",
        workspace_dir: str = "",
    ) -> None:
        self._working_dir = working_dir
        self._repo_url = repo_url
        self._branch = branch
        self._callback_url = callback_url
        self._api_key = api_key
        self._krewhub_url = krewhub_url
        self._recipe_contexts = recipe_contexts or {}
        self._workspace_dir = workspace_dir or working_dir
        self._sessions: dict[str, SpawnSession] = {}
        # Cache one HookWiring per agent type. We write each writer
        # once at startup and reuse the result on every spawn — exactly
        # the way vibe-island writes its global configs at first launch.
        self._wirings: dict[str, HookWiring] = {}

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
        bundle_id: str = "",
        recipe_id: str = "",
        krewhub_url: str = "",
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

        resolved_workdir = working_dir or self._working_dir

        # Resolve the per-agent hook wiring (writes the config file
        # the first time this agent type is spawned, then caches it).
        wiring = self._wirings.get(agent_name)
        if wiring is None:
            try:
                wiring = write_hooks_for(agent_name, self._workspace_dir)
            except Exception:  # noqa: BLE001 — never block the spawn
                logger.exception(
                    "SpawnManager: writer failed for %s in %s",
                    agent_name, self._workspace_dir,
                )
                wiring = None
            if wiring is not None:
                self._wirings[agent_name] = wiring

        # Build env vars consumed by `krewcli bridge`.
        hook_env = build_hook_env(
            task_id=task_id,
            bundle_id=bundle_id,
            recipe_id=recipe_id,
            agent_id=agent_id,
            krewhub_url=krewhub_url or self._krewhub_url,
            api_key=self._api_key,
        )
        # Layer the wiring's per-agent env on top so each agent
        # runner can find its own settings/plugin file.
        if wiring is not None:
            hook_env.update(wiring.env)

        session.task = asyncio.create_task(
            self._run_and_report(
                session, prompt,
                working_dir=resolved_workdir,
                repo_url=repo_url or self._repo_url,
                branch=branch or self._branch,
                hook_env=hook_env,
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
        hook_env: dict[str, str] | None = None,
    ) -> None:
        """Execute CLI agent and report results to krewhub callback."""
        result = await self._execute(
            session.agent_name, prompt,
            working_dir=working_dir, repo_url=repo_url, branch=branch,
            hook_env=hook_env or {},
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
        hook_env: dict[str, str] | None = None,
    ) -> SpawnResult:
        """Run the agent CLI and collect results."""
        try:
            agent = get_agent(agent_name)
            deps = AgentDeps(
                working_dir=working_dir or self._working_dir,
                repo_url=repo_url or self._repo_url,
                branch=branch or self._branch,
                context=hook_env or {},
            )
            run_result: AgentRunResult = await agent.run(prompt, deps=deps)
            task_result: TaskResult = run_result.output

            return SpawnResult(
                task_id="",
                agent_id="",
                success=task_result.success,
                summary=task_result.summary,
                full_output=task_result.full_output,
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
            "full_output": result.full_output,
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

    async def run_codegen(
        self,
        agent_name: str,
        user_prompt: str,
        agents_summary: str,
        *,
        working_dir: str = "",
        repo_url: str = "",
        branch: str = "main",
    ) -> str | None:
        """Run a CLI agent once with the graph-codegen prompt template.

        Used by GatewayExecutor when a worker receives a planning request
        (a message with ``bundle_id`` metadata but no ``task_id``). Unlike
        the normal spawn path this is *synchronous-ish*: we await the
        agent's run, capture its stdout from the returned TaskResult's
        ``full_output``, strip markdown fences, and hand the code back
        to the caller to POST to /bundles/{id}/graph.

        No krewhub task is created. No callback is fired. The agent runs
        in-process alongside the executor that received the request.

        Args:
            agent_name: Registry key ("claude", "codex", "bub", ...).
            user_prompt: The original user prompt from the empty bundle.
            agents_summary: Short human-readable list of agents available
                in the cookbook, interpolated into the codegen template
                so the LLM knows which task_kinds it can target.
            working_dir/repo_url/branch: Per-recipe context, same as
                the normal spawn path.

        Returns:
            The raw graph source code on success, or None on failure
            (CLI crash, empty output, malformed fences).
        """
        from krewcli.workflows.llm_planner import CODEGEN_PROMPT, _clean_code

        codegen_prompt = CODEGEN_PROMPT.format(
            prompt=user_prompt, agents=agents_summary,
        )
        result = await self._execute(
            agent_name, codegen_prompt,
            working_dir=working_dir,
            repo_url=repo_url,
            branch=branch,
            hook_env={},
        )
        if not result.success:
            logger.warning(
                "SpawnManager.run_codegen: %s failed: %s",
                agent_name, result.blocked_reason or result.summary,
            )
            return None

        raw_output = result.full_output or result.summary or ""
        if not raw_output.strip():
            logger.warning(
                "SpawnManager.run_codegen: %s returned empty output",
                agent_name,
            )
            return None

        code = _clean_code(raw_output)
        if "g.build()" not in code and "graph = " not in code:
            logger.warning(
                "SpawnManager.run_codegen: %s output does not look like "
                "graph code (no g.build() or graph = assignment)",
                agent_name,
            )
            return None
        return code

    async def shutdown(self) -> None:
        """Cancel all running sessions."""
        for task_id in list(self._sessions):
            await self.cancel(task_id)
