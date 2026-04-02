from __future__ import annotations

import logging
import uuid

from krewcli.agents.base import AgentDeps
from krewcli.agents.models import TaskResult
from krewcli.agents.registry import get_agent
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.presence.heartbeat import HeartbeatLoop
from krewcli.runtime.interface import AgentRuntimeInterface, TaskRunSpec, TaskRunResult

logger = logging.getLogger(__name__)


class TaskRunner:
    """Claim-execute-milestone-done workflow for a single task.

    Supports two modes:
    1. Legacy mode: uses agent registry + AgentDeps directly
    2. CRI mode: uses AgentRuntimeInterface for execution

    Both modes share the same claim/milestone/status reporting flow.
    """

    def __init__(
        self,
        client: KrewHubClient,
        heartbeat: HeartbeatLoop,
        agent_name: str,
        agent_id: str,
        working_dir: str,
        repo_url: str,
        branch: str,
        runtime: AgentRuntimeInterface | None = None,
    ) -> None:
        self._client = client
        self._heartbeat = heartbeat
        self._agent_name = agent_name
        self._agent_id = agent_id
        self._working_dir = working_dir
        self._repo_url = repo_url
        self._branch = branch
        self._runtime = runtime

    async def claim_and_execute(self, task_id: str) -> TaskResult | None:
        """Claim a task, execute it, and report results."""

        try:
            task_data = await self._client.claim_task(task_id, self._agent_id)
        except Exception as exc:
            logger.error("Failed to claim task %s: %s", task_id, exc)
            return None

        self._heartbeat.current_task_id = task_id
        logger.info("Claimed task %s: %s", task_id, task_data.get("title", ""))

        try:
            if self._runtime is not None:
                task_result = await self._execute_via_runtime(task_data)
            else:
                task_result = await self._execute_via_legacy(task_data)

            await self._report_results(task_id, task_result)
            return task_result

        except Exception as exc:
            logger.error("Task %s execution failed: %s", task_id, exc)
            try:
                await self._client.update_task_status(
                    task_id, "blocked", blocked_reason=f"Execution error: {exc}"
                )
            except Exception:
                pass
            return None

        finally:
            self._heartbeat.current_task_id = None

    async def _execute_via_runtime(self, task_data: dict) -> TaskResult:
        """Execute using the formal AgentRuntimeInterface (CRI)."""
        spec = TaskRunSpec(
            task_id=task_data.get("id", ""),
            title=task_data.get("title", ""),
            description=task_data.get("description", ""),
            working_dir=self._working_dir,
            repo_url=self._repo_url,
            branch=self._branch,
        )

        run_result = await self._runtime.run_task(spec)

        return TaskResult(
            summary=run_result.summary,
            files_modified=run_result.files_modified,
            facts=run_result.facts,
            code_refs=run_result.code_refs,
            success=run_result.success,
            blocked_reason=run_result.blocked_reason,
        )

    async def _execute_via_legacy(self, task_data: dict) -> TaskResult:
        """Execute using the legacy agent registry + AgentDeps."""
        agent = get_agent(self._agent_name)
        deps = AgentDeps(
            working_dir=self._working_dir,
            repo_url=self._repo_url,
            branch=self._branch,
        )

        prompt = (
            f"Complete this task: {task_data.get('title', '')}\n"
            f"Description: {task_data.get('description', 'No description')}\n"
            f"Working directory: {self._working_dir}\n"
            f"Repository: {self._repo_url} branch: {self._branch}"
        )

        result = await agent.run(prompt, deps=deps)
        return result.output

    async def _report_results(self, task_id: str, task_result: TaskResult) -> None:
        """Post milestone and update task status."""
        facts = [
            {
                "id": f"f_{uuid.uuid4().hex[:8]}",
                "claim": f.claim,
                "source_url": f.source_url,
                "source_title": f.source_title,
                "captured_by": self._agent_id,
                "confidence": f.confidence,
            }
            for f in task_result.facts
        ]
        code_refs = [c.model_dump() for c in task_result.code_refs]

        await self._client.post_event(
            task_id=task_id,
            event_type="milestone",
            actor_id=self._agent_id,
            body=task_result.summary,
            facts=facts,
            code_refs=code_refs,
        )

        if task_result.success:
            await self._client.update_task_status(task_id, "done")
            logger.info("Task %s completed", task_id)
        else:
            await self._client.update_task_status(
                task_id, "blocked",
                blocked_reason=task_result.blocked_reason or "Agent reported failure",
            )
            logger.warning("Task %s blocked: %s", task_id, task_result.blocked_reason)
