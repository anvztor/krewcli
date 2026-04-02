"""Tier 1 Agent: Direct LLM call (stateless prediction).

Uses pydantic-ai Agent for a single-turn LLM call with no tools.
Good for: summarization, classification, planning, code review.
"""

from __future__ import annotations

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
    TaskArtifactUpdateEvent,
)
from a2a.utils.artifact import new_text_artifact
from a2a.utils.message import new_agent_text_message
from a2a.utils.task import new_task

from pydantic_ai import Agent


class DirectLLMExecutor(AgentExecutor):
    """Tier 1: Stateless LLM call. Prompt in, text out. No tools."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._agent = Agent(model, result_type=str)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=context.task_id, context_id=context.context_id,
            final=False, status=TaskStatus(state=TaskState.working),
        ))

        prompt = _extract_text(context)

        try:
            result = await self._agent.run(prompt)

            await event_queue.enqueue_event(TaskArtifactUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                artifact=new_text_artifact(name="result", text=result.output),
            ))
            await event_queue.enqueue_event(TaskStatusUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                final=True, status=TaskStatus(state=TaskState.completed),
            ))
        except Exception as exc:
            await event_queue.enqueue_event(TaskStatusUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                final=True, status=TaskStatus(
                    state=TaskState.failed,
                    message=new_agent_text_message(f"LLM call failed: {exc}"),
                ),
            ))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=context.task_id, context_id=context.context_id,
            final=True, status=TaskStatus(state=TaskState.canceled),
        ))


def build_direct_llm_card(provider: str, host: str, port: int) -> AgentCard:
    return AgentCard(
        name=f"llm:{provider}",
        description=f"Stateless LLM predictor via {provider}. No tools, no state.",
        url=f"http://{host}:{port}",
        version="0.2.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(transport="JSONRPC", url=f"http://{host}:{port}")],
        skills=[AgentSkill(
            id=f"predict:{provider}",
            name=f"LLM Prediction ({provider})",
            description="Stateless prediction: summarize, classify, plan, review.",
            tags=["summarize", "classify", "plan", "review"],
            examples=["Summarize this PR", "Classify task complexity", "Review this code"],
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
