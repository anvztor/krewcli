from __future__ import annotations

import logging
import uuid

from krewcli.agents.models import TaskResult
from krewcli.agents.registry import get_agent, AgentDeps
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.presence.heartbeat import HeartbeatLoop

logger = logging.getLogger(__name__)


class TaskRunner:
    """Claim-execute-milestone-done workflow for a single task."""

    def __init__(
        self,
        client: KrewHubClient,
        heartbeat: HeartbeatLoop,
        agent_name: str,
        agent_id: str,
        working_dir: str,
        repo_url: str,
        branch: str,
    ) -> None:
        self._client = client
        self._heartbeat = heartbeat
        self._agent_name = agent_name
        self._agent_id = agent_id
        self._working_dir = working_dir
        self._repo_url = repo_url
        self._branch = branch

    async def claim_and_execute(self, task_id: str) -> TaskResult | None:
        """Claim a task, execute it with the pydantic-ai agent, and report results."""

        try:
            task_data = await self._client.claim_task(task_id, self._agent_id)
        except Exception as exc:
            logger.error("Failed to claim task %s: %s", task_id, exc)
            return None

        self._heartbeat.current_task_id = task_id
        logger.info("Claimed task %s: %s", task_id, task_data.get("title", ""))

        try:
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
            task_result: TaskResult = result.output

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
