"""Tier 3 Agent: Orchestrator using pydantic-graph workflow templates.

No LLM needed for decomposition. The graph structure IS the plan.
Graph nodes become tasks, edges become dependencies.
graph.mermaid_code() renders the workflow diagram.
"""

from __future__ import annotations

import json
import logging

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

from krewcli.workflows import get_workflow

logger = logging.getLogger(__name__)


class OrchestratorExecutor(AgentExecutor):
    """Tier 3: pydantic-graph orchestrator.

    Receives a prompt, selects the right workflow template,
    extracts tasks with dependency edges, renders mermaid diagram.
    Returns structured task specs + mermaid as artifact.
    """

    def __init__(self, **kwargs) -> None:
        pass

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=context.task_id, context_id=context.context_id,
            final=False, status=TaskStatus(
                state=TaskState.working,
                message=new_agent_text_message("Orchestrator: selecting workflow..."),
            ),
        ))

        prompt = _extract_text(context)

        try:
            spec = get_workflow(prompt)

            result = {
                "workflow": spec.workflow_name,
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "description": t.description,
                        "dependsOn": t.depends_on,
                    }
                    for t in spec.tasks
                ],
                "mermaid": spec.mermaid,
            }

            await event_queue.enqueue_event(TaskArtifactUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                artifact=new_text_artifact(
                    name="result",
                    text=json.dumps(result, indent=2),
                ),
            ))
            await event_queue.enqueue_event(TaskStatusUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                final=True, status=TaskStatus(state=TaskState.completed),
            ))

        except Exception as exc:
            logger.exception("Orchestrator failed")
            await event_queue.enqueue_event(TaskStatusUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                final=True, status=TaskStatus(
                    state=TaskState.failed,
                    message=new_agent_text_message(f"Orchestrator failed: {exc}"),
                ),
            ))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=context.task_id, context_id=context.context_id,
            final=True, status=TaskStatus(state=TaskState.canceled),
        ))


def build_orchestrator_card(host: str, port: int) -> AgentCard:
    return AgentCard(
        name="orchestrator",
        description="Decomposes complex prompts into sub-tasks, dispatches to krewhub, "
        "monitors completion, and synthesizes results.",
        url=f"http://{host}:{port}",
        version="0.2.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(transport="JSONRPC", url=f"http://{host}:{port}")],
        skills=[AgentSkill(
            id="orchestrate",
            name="Orchestrator",
            description="Decompose complex requests into sub-tasks and coordinate multi-agent execution.",
            tags=["orchestrate", "decompose", "coordinate", "synthesize"],
            examples=[
                "Build an authentication system with login, signup, and password reset",
                "Refactor the database layer to use connection pooling",
            ],
        )],
    )


def _extract_text(context: RequestContext) -> str:
    if context.message and context.message.parts:
        for part in context.message.parts:
            if hasattr(part, "root") and hasattr(part.root, "text"):
                return part.root.text
            if hasattr(part, "text"):
                return part.text
    return ""
