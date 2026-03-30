from __future__ import annotations

import json

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.artifact import new_text_artifact
from a2a.utils.message import new_agent_text_message
from a2a.utils.task import new_task

from krewcli.agents.registry import get_agent, AgentDeps
from krewcli.agents.models import TaskResult


class KrewAgentExecutor(AgentExecutor):
    """Routes incoming A2A messages to the appropriate pydantic-ai agent."""

    def __init__(
        self,
        default_agent_name: str,
        working_dir: str,
        repo_url: str = "",
        branch: str = "main",
    ) -> None:
        self._default_agent_name = default_agent_name
        self._working_dir = working_dir
        self._repo_url = repo_url
        self._branch = branch

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(
            _new_status_event(
                task_id=context.task_id,
                context_id=context.context_id,
                state=TaskState.working,
                final=False,
                message=new_agent_text_message(
                    f"Delegating to {self._default_agent_name}..."
                ),
            )
        )

        user_message = _extract_text(context)
        agent_name = self._resolve_agent(user_message)

        try:
            agent = get_agent(agent_name)
            deps = AgentDeps(
                working_dir=self._working_dir,
                repo_url=self._repo_url,
                branch=self._branch,
            )
            result = await agent.run(user_message, deps=deps)
            task_result: TaskResult = result.output

            artifact_text = json.dumps(task_result.model_dump(), indent=2)
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=new_text_artifact(name="result", text=artifact_text),
                )
            )
            await event_queue.enqueue_event(
                _new_status_event(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    state=TaskState.completed,
                    final=True,
                )
            )

        except Exception as exc:
            await event_queue.enqueue_event(
                _new_status_event(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    state=TaskState.failed,
                    final=True,
                    message=new_agent_text_message(f"Error: {exc}"),
                )
            )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        await event_queue.enqueue_event(
            _new_status_event(
                task_id=context.task_id,
                context_id=context.context_id,
                state=TaskState.canceled,
                final=True,
                message=new_agent_text_message("Task cancelled."),
            )
        )

    def _resolve_agent(self, message: str) -> str:
        """Parse agent name from message prefix like 'use codex: ...' or fall back to default."""
        lower = message.lower().strip()
        for prefix in ("use codex:", "use claude:", "use bub:"):
            if lower.startswith(prefix):
                return prefix.split()[1].rstrip(":")
        return self._default_agent_name


def _extract_text(context: RequestContext) -> str:
    """Extract text content from the A2A request message."""
    if context.message and context.message.parts:
        for part in context.message.parts:
            if hasattr(part, "root") and hasattr(part.root, "text"):
                return part.root.text
            if hasattr(part, "text"):
                return part.text
    return ""


def _new_status_event(
    task_id: str | None,
    context_id: str | None,
    state: TaskState,
    final: bool,
    message=None,
) -> TaskStatusUpdateEvent:
    return TaskStatusUpdateEvent(
        task_id=task_id,
        context_id=context_id,
        final=final,
        status=TaskStatus(state=state, message=message),
    )
