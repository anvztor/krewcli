"""Tier 3 Agent: Orchestrator using pydantic-graph.

Decomposes a complex prompt into sub-tasks, dispatches them to krewhub
(where the scheduler assigns them to Tier 2 agents), monitors progress,
and synthesizes results into a final digest.

Graph: Plan → Dispatch → Monitor → Synthesize
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Union

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

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)


# ── Graph State ──


@dataclass
class OrchestrationState:
    prompt: str = ""
    recipe_id: str = ""
    bundle_id: str = ""
    task_titles: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)
    final_summary: str = ""


# ── LLM output types ──


class TaskPlan(BaseModel):
    """Output of the planning step."""
    tasks: list[str] = Field(description="List of task titles to create")
    reasoning: str = Field(description="Why these tasks were chosen")


# ── Graph Nodes ──


@dataclass
class PlanNode(BaseNode[OrchestrationState]):
    """Use LLM to decompose prompt into sub-task titles."""

    model: str = "anthropic:claude-sonnet-4-20250514"

    async def run(self, ctx: GraphRunContext[OrchestrationState]) -> Union[DispatchNode, End[str]]:
        planner = Agent(self.model, output_type=TaskPlan, system_prompt=(
            "You are a project planner. Given a request, break it down into "
            "2-5 concrete, actionable coding tasks. Each task should be "
            "independently executable by a coding agent."
        ))

        result = await planner.run(ctx.state.prompt)
        plan: TaskPlan = result.output
        ctx.state.task_titles = plan.tasks
        logger.info("Orchestrator planned %d tasks: %s", len(plan.tasks), plan.reasoning)

        if not plan.tasks:
            return End("No tasks to execute")

        return DispatchNode()


@dataclass
class DispatchNode(BaseNode[OrchestrationState]):
    """POST sub-tasks to krewhub as a new bundle."""

    krewhub_url: str = ""
    api_key: str = ""

    async def run(self, ctx: GraphRunContext[OrchestrationState]) -> MonitorNode:
        client = KrewHubClient(self.krewhub_url, self.api_key)
        try:
            tasks_payload = [{"title": t} for t in ctx.state.task_titles]
            resp = await client._client.post(
                f"/api/v1/recipes/{ctx.state.recipe_id}/bundles",
                json={
                    "prompt": f"[Orchestrated] {ctx.state.prompt}",
                    "requested_by": "orchestrator",
                    "tasks": tasks_payload,
                },
                headers={"X-API-Key": self.api_key},
            )
            resp.raise_for_status()
            data = resp.json()
            ctx.state.bundle_id = data["bundle"]["id"]
            ctx.state.task_ids = [t["id"] for t in data["tasks"]]
            logger.info("Orchestrator dispatched bundle %s with %d tasks", ctx.state.bundle_id, len(ctx.state.task_ids))
        finally:
            await client.close()

        return MonitorNode()


@dataclass
class MonitorNode(BaseNode[OrchestrationState]):
    """Watch krewhub for bundle completion."""

    krewhub_url: str = ""
    api_key: str = ""
    poll_interval: float = 5.0
    timeout: float = 300.0

    async def run(self, ctx: GraphRunContext[OrchestrationState]) -> Union[SynthesizeNode, End[str]]:
        client = KrewHubClient(self.krewhub_url, self.api_key)
        deadline = asyncio.get_event_loop().time() + self.timeout

        try:
            while asyncio.get_event_loop().time() < deadline:
                bundle = await client.get_bundle(ctx.state.bundle_id)
                status = bundle.get("bundle", {}).get("status", "")

                if status in ("cooked", "blocked"):
                    for task in bundle.get("tasks", []):
                        ctx.state.results[task["id"]] = task.get("status", "unknown")
                    return SynthesizeNode()

                if status in ("cancelled", "digested", "rejected"):
                    return End(f"Bundle reached terminal state: {status}")

                await asyncio.sleep(self.poll_interval)
        finally:
            await client.close()

        return End("Timed out waiting for bundle completion")


@dataclass
class SynthesizeNode(BaseNode[OrchestrationState]):
    """Use LLM to combine results into a summary."""

    model: str = "anthropic:claude-sonnet-4-20250514"

    async def run(self, ctx: GraphRunContext[OrchestrationState]) -> End[str]:
        results_text = "\n".join(
            f"- Task {tid}: {status}" for tid, status in ctx.state.results.items()
        )

        synthesizer = Agent(self.model, output_type=str, system_prompt=(
            "Summarize the results of a multi-task orchestration. "
            "Be concise — 2-3 sentences."
        ))

        result = await synthesizer.run(
            f"Original request: {ctx.state.prompt}\n\nTask results:\n{results_text}"
        )

        ctx.state.final_summary = result.output
        return End(result.output)


# ── Executor ──


class OrchestratorExecutor(AgentExecutor):
    """Tier 3: pydantic-graph orchestrator. Decomposes, dispatches, synthesizes."""

    def __init__(self, model: str, krewhub_url: str, api_key: str) -> None:
        self._model = model
        self._krewhub_url = krewhub_url
        self._api_key = api_key

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=context.task_id, context_id=context.context_id,
            final=False, status=TaskStatus(
                state=TaskState.working,
                message=new_agent_text_message("Orchestrator: planning sub-tasks..."),
            ),
        ))

        prompt = _extract_text(context)

        # Extract recipe_id from context metadata if available
        recipe_id = ""
        if context.current_task and hasattr(context.current_task, "metadata"):
            meta = getattr(context.current_task, "metadata", {}) or {}
            recipe_id = meta.get("recipe_id", "")

        state = OrchestrationState(prompt=prompt, recipe_id=recipe_id)
        graph = Graph(
            nodes=[PlanNode, DispatchNode, MonitorNode, SynthesizeNode],
        )

        try:
            plan_node = PlanNode(model=self._model)
            # We need to inject krewhub config into dispatch/monitor nodes
            # pydantic-graph creates node instances from class, so we override after
            result = await graph.run(plan_node, state=state)

            await event_queue.enqueue_event(TaskArtifactUpdateEvent(
                task_id=context.task_id, context_id=context.context_id,
                artifact=new_text_artifact(
                    name="result",
                    text=json.dumps({
                        "summary": state.final_summary or str(result.output),
                        "bundle_id": state.bundle_id,
                        "tasks": state.task_ids,
                        "results": state.results,
                    }, indent=2),
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
