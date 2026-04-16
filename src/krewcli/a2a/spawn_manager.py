"""Spawn manager — launches and tracks CLI agent subprocesses.

The gateway executor delegates to SpawnManager to run CLI agents
(claude, codex, bub) as on-demand subprocesses. Each task gets its
own process that starts, executes, and exits.

Reports results back to krewhub via the A2A callback endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from krewcli.agents.base import AgentDeps, AgentRunResult
from krewcli.agents.event_sink import (
    EventSink,
    KrewhubEventSink,
    MILESTONE,
    NullEventSink,
)
from krewcli.agents.models import TaskResult
from krewcli.agents.registry import get_agent

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)

_STREAM_EVENTS_ENABLED = os.getenv("KREWCLI_STREAM_EVENTS", "1") != "0"


@dataclass
class SpawnSession:
    """Tracks a running CLI agent subprocess."""
    task_id: str
    agent_name: str
    agent_id: str
    task: asyncio.Task | None = None
    # Background task that polls krewhub for cancellation and signals
    # the main task by setting cancel_event. Cleaned up on spawn completion.
    cancel_watcher: asyncio.Task | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


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
        krewhub_client: "KrewHubClient | None" = None,
    ) -> None:
        self._working_dir = working_dir
        self._repo_url = repo_url
        self._branch = branch
        self._callback_url = callback_url
        self._api_key = api_key
        self._recipe_contexts = recipe_contexts or {}
        self._sessions: dict[str, SpawnSession] = {}
        self._krewhub_client = krewhub_client

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

        # Start the cancel-watcher. It polls krewhub for cancellation
        # every 5 seconds and cancels the spawn task if the server marks
        # this task as cancelled (e.g. via bundle cancel).
        if self._krewhub_client is not None:
            session.cancel_watcher = asyncio.create_task(
                self._watch_cancellation(session),
                name=f"cancel_watch:{task_id}",
            )

        logger.info(
            "SpawnManager: spawned %s for task %s", agent_name, task_id,
        )
        return True

    async def _watch_cancellation(self, session: SpawnSession) -> None:
        """Poll krewhub for cancellation, signal the main spawn task on hit.

        Polls every 5s. When the task is marked cancelled server-side
        (e.g. user clicks Cancel on the bundle), this loop wakes up,
        cancels the spawn asyncio.Task, and signals the event so any
        final cleanup can complete quickly.
        """
        import httpx
        poll_interval = 5.0
        client = self._krewhub_client
        if client is None:
            return
        try:
            while True:
                await asyncio.sleep(poll_interval)
                # Session may have been removed (task completed)
                if session.task_id not in self._sessions:
                    return
                try:
                    # Using the raw http client from KrewHubClient
                    resp = await client._client.get(  # type: ignore[attr-defined]
                        f"/api/v1/tasks/{session.task_id}/cancel-status",
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("cancelled"):
                            logger.warning(
                                "SpawnManager: task %s cancelled by server, killing subprocess",
                                session.task_id,
                            )
                            session.cancel_event.set()
                            if session.task is not None and not session.task.done():
                                session.task.cancel()
                            return
                except httpx.HTTPError as exc:
                    logger.debug(
                        "SpawnManager: cancel-status poll failed for %s: %s",
                        session.task_id, exc,
                    )
        except asyncio.CancelledError:
            return

    async def _run_and_report(
        self,
        session: SpawnSession,
        prompt: str,
        working_dir: str = "",
        repo_url: str = "",
        branch: str = "main",
    ) -> None:
        """Execute CLI agent and report results to krewhub callback."""
        sink = self._build_event_sink(session)
        was_cancelled = False
        try:
            result = await self._execute(
                session.agent_name, prompt,
                working_dir=working_dir, repo_url=repo_url, branch=branch,
                event_sink=sink,
            )
        except asyncio.CancelledError:
            # Cancel watcher tripped — emit a marker event and report
            # a failed result so the server knows execution stopped.
            was_cancelled = True
            logger.info(
                "SpawnManager: task %s cancelled mid-execution",
                session.task_id,
            )
            result = SpawnResult(
                task_id=session.task_id,
                agent_id=session.agent_id,
                success=False,
                summary="Task cancelled by user",
                blocked_reason="cancelled",
            )
        finally:
            await sink.flush()
            # Stop the cancel watcher (no-op if it already exited)
            if session.cancel_watcher is not None and not session.cancel_watcher.done():
                session.cancel_watcher.cancel()

        result.task_id = session.task_id
        result.agent_id = session.agent_id

        self._sessions.pop(session.task_id, None)

        if self._callback_url:
            await self._report_callback(result)

        logger.info(
            "SpawnManager: task %s finished (success=%s, cancelled=%s)",
            session.task_id, result.success, was_cancelled,
        )

    def _build_event_sink(self, session: SpawnSession) -> EventSink:
        """Construct an event sink for a spawn session."""
        return self.build_task_event_sink(
            task_id=session.task_id, agent_id=session.agent_id,
        )

    def build_task_event_sink(self, task_id: str, agent_id: str) -> EventSink:
        """Public helper — build a task-scoped event sink.

        Returns a NullEventSink unless streaming is enabled AND a
        KrewHubClient is available. The KrewhubEventSink batches emits
        and POSTs them to /tasks/{id}/events:batch so cookrew can
        render tool calls and assistant replies live.

        Used both by the SpawnSession path and by gateway handlers that
        drive _execute directly and need a sink without a full session.
        """
        if not _STREAM_EVENTS_ENABLED or self._krewhub_client is None:
            return NullEventSink()
        if not task_id:
            # KrewhubEventSink POSTs to /api/v1/tasks/{id}/events:batch —
            # with an empty task_id those calls 404. Callers without a
            # task scope (e.g. planner codegen) get a null sink instead.
            return NullEventSink()
        return KrewhubEventSink(
            client=self._krewhub_client,
            task_id=task_id,
            agent_id=agent_id,
        )

    async def _execute(
        self,
        agent_name: str,
        prompt: str,
        working_dir: str = "",
        repo_url: str = "",
        branch: str = "main",
        event_sink: EventSink | None = None,
        context: dict[str, str] | None = None,
    ) -> SpawnResult:
        """Run the agent CLI and collect results.

        `context` becomes `AgentDeps.context` and propagates to subprocess
        env for CLI-backed agents. The codex rollout watcher needs
        KREWHUB_TASK_ID / KREWHUB_URL / KREWHUB_API_KEY in context to
        forward tool_use / thinking events back to krewhub.
        """
        try:
            agent = get_agent(agent_name)
            deps = AgentDeps(
                working_dir=working_dir or self._working_dir,
                repo_url=repo_url or self._repo_url,
                branch=branch or self._branch,
                event_sink=event_sink,
                context=context or {},
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

    async def shutdown(self) -> None:
        """Cancel all running sessions."""
        for task_id in list(self._sessions):
            await self.cancel(task_id)
