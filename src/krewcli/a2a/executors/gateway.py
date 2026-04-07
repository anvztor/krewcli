"""Gateway executor — A2A executor that spawns CLI agents on demand.

Each agent type (claude, codex, bub) gets its own GatewayExecutor
instance, mounted at a separate A2A subpath. When krewhub dispatches
a task via A2A message/send, the executor checks capacity and spawns
the appropriate CLI subprocess.
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
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.message import new_agent_text_message
from a2a.utils.task import new_task

from krewcli.a2a.spawn_manager import SpawnManager
from krewcli.agents.registry import AGENT_REGISTRY


class GatewayExecutor(AgentExecutor):
    """A2A executor that spawns CLI agents on demand.

    When execute() is called, it:
    1. Extracts the task prompt from the A2A message
    2. Reads task_id from metadata (set by krewhub's dispatcher)
    3. Checks capacity
    4. Spawns the CLI agent via SpawnManager
    5. Returns "working" immediately (async execution)
    """

    def __init__(
        self,
        agent_name: str,
        spawn_manager: SpawnManager,
        agent_id: str,
        max_concurrent: int = 1,
    ) -> None:
        self._agent_name = agent_name
        self._spawn = spawn_manager
        self._agent_id = agent_id
        self._max_concurrent = max_concurrent

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        # Check capacity
        running = self._spawn.running_count_for(self._agent_name)
        if running >= self._max_concurrent:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.rejected,
                    final=True,
                    message=new_agent_text_message(
                        f"At capacity ({running}/{self._max_concurrent})"
                    ),
                )
            )
            return

        # Extract prompt and task metadata
        prompt = _extract_text(context)
        metadata = _extract_metadata(context)
        krewhub_task_id = metadata.get("task_id", context.task_id)

        # Resolve per-recipe working directory
        recipe_name = metadata.get("recipe_name", "")
        recipe_ctx = self._spawn.resolve_recipe_context(recipe_name) if recipe_name else {}

        if not prompt:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.failed,
                    final=True,
                    message=new_agent_text_message("No prompt provided"),
                )
            )
            return

        # Spawn the CLI agent
        started = await self._spawn.spawn(
            agent_name=self._agent_name,
            agent_id=self._agent_id,
            task_id=krewhub_task_id,
            prompt=prompt,
            working_dir=recipe_ctx.get("working_dir"),
            repo_url=recipe_ctx.get("repo_url"),
            branch=recipe_ctx.get("branch"),
            bundle_id=metadata.get("bundle_id", ""),
            recipe_id=metadata.get("recipe_id", ""),
        )

        if started:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.working,
                    final=False,
                    message=new_agent_text_message(
                        f"Spawned {self._agent_name} CLI for task {krewhub_task_id}"
                    ),
                )
            )
        else:
            await event_queue.enqueue_event(
                _status_event(
                    context,
                    TaskState.failed,
                    final=True,
                    message=new_agent_text_message(
                        f"Failed to spawn {self._agent_name}"
                    ),
                )
            )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        metadata = _extract_metadata(context)
        krewhub_task_id = metadata.get("task_id", context.task_id)
        await self._spawn.cancel(krewhub_task_id)
        await event_queue.enqueue_event(
            _status_event(
                context,
                TaskState.canceled,
                final=True,
                message=new_agent_text_message("Task cancelled."),
            )
        )


def build_gateway_agent_card(
    agent_name: str,
    host: str,
    port: int,
) -> AgentCard:
    """Build an AgentCard for a gateway-managed CLI agent."""
    entry = AGENT_REGISTRY.get(agent_name, {})
    display_name = entry.get("display_name", agent_name)
    capabilities_list = entry.get("capabilities", [])

    base_url = f"http://{host}:{port}/agents/{agent_name}"

    return AgentCard(
        name=f"gateway:{agent_name}",
        description=(
            f"On-demand {display_name} agent. Spawns a CLI subprocess "
            f"for each task. Stateful: reads/writes files, git state."
        ),
        url=base_url,
        version="0.3.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(transport="JSONRPC", url=base_url),
        ],
        skills=[
            AgentSkill(
                id=f"gateway:{agent_name}",
                name=display_name,
                description=f"Execute coding tasks using {display_name} CLI (on-demand spawn)",
                tags=["code", "implement", "fix", "test", "refactor"] + capabilities_list,
                examples=[
                    "Implement a heartbeat endpoint",
                    "Fix failing tests in the auth module",
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


def _extract_metadata(context: RequestContext) -> dict:
    if context.message and hasattr(context.message, "metadata"):
        meta = context.message.metadata
        if isinstance(meta, dict):
            return meta
    return {}


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
