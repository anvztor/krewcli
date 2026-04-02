"""Tier 2 Agent: CLI subprocess executor.

Wraps local CLI tools (claude, codex, bub) behind the A2A AgentExecutor
interface. Each invocation spawns the CLI, captures output, and reports
results as A2A task events.
"""

from __future__ import annotations

import json

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.artifact import new_text_artifact
from a2a.utils.message import new_agent_text_message
from a2a.utils.task import new_task

from krewcli.agents.base import AgentDeps
from krewcli.agents.models import TaskResult
from krewcli.agents.registry import get_agent, AGENT_REGISTRY


class CLIExecutor(AgentExecutor):
    """Tier 2: Spawn a local CLI agent (claude, codex, bub) as a subprocess."""

    def __init__(
        self,
        agent_name: str,
        working_dir: str,
        repo_url: str = "",
        branch: str = "main",
    ) -> None:
        self._agent_name = agent_name
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
            _status_event(
                context,
                TaskState.working,
                final=False,
                message=new_agent_text_message(
                    f"Executing with {self._agent_name} CLI..."
                ),
            )
        )

        prompt = _extract_text(context)

        try:
            agent = get_agent(self._agent_name)
            deps = AgentDeps(
                working_dir=self._working_dir,
                repo_url=self._repo_url,
                branch=self._branch,
            )
            result = await agent.run(prompt, deps=deps)
            task_result: TaskResult = result.output

            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=new_text_artifact(
                        name="result",
                        text=json.dumps(task_result.model_dump(), indent=2),
                    ),
                )
            )
            await event_queue.enqueue_event(
                _status_event(context, TaskState.completed, final=True)
            )

        except Exception as exc:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.failed,
                    final=True,
                    message=new_agent_text_message(f"Error: {exc}"),
                )
            )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        await event_queue.enqueue_event(
            _status_event(
                context,
                TaskState.canceled,
                final=True,
                message=new_agent_text_message("Task cancelled."),
            )
        )


def build_cli_agent_card(
    agent_name: str,
    host: str,
    port: int,
) -> AgentCard:
    """Build an AgentCard for a CLI-based agent."""
    entry = AGENT_REGISTRY.get(agent_name, {})
    display_name = entry.get("display_name", agent_name)
    capabilities_list = entry.get("capabilities", [])

    return AgentCard(
        name=f"cli:{agent_name}",
        description=f"Coding agent powered by {display_name} CLI. "
        f"Stateful: reads/writes files, runs commands, modifies git state.",
        url=f"http://{host}:{port}",
        version="0.2.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(transport="JSONRPC", url=f"http://{host}:{port}"),
        ],
        skills=[
            AgentSkill(
                id=f"code:{agent_name}",
                name=display_name,
                description=f"Execute coding tasks using {display_name}",
                tags=["code", "implement", "fix", "test", "refactor"] + capabilities_list,
                examples=[
                    f"Implement a heartbeat endpoint",
                    f"Fix failing tests in the auth module",
                ],
            ),
        ],
    )


def _extract_text(context: RequestContext) -> str:
    if context.message and context.message.parts:
        for part in context.message.parts:
            if hasattr(part, "root") and hasattr(part.root, "text"):
                return part.root.text
            if hasattr(part, "text"):
                return part.text
    return ""


def _status_event(
    context: RequestContext,
    state: TaskState,
    final: bool,
    message=None,
) -> TaskStatusUpdateEvent:
    return TaskStatusUpdateEvent(
        task_id=context.task_id,
        context_id=context.context_id,
        final=final,
        status=TaskStatus(state=state, message=message),
    )
