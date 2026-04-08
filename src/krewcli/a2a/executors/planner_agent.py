"""PlannerOrchestratorExecutor — slim graph-code generator.

This is the krewhub-driven replacement for the old monolithic
OrchestratorExecutor. It does *only* code generation and hands the
result off to krewhub via the attach-graph endpoint. Krewhub then
validates, renders, creates tasks, and runs the graph through its
GraphRunnerController. No bundle creation, no in-process graph
execution, no GraphExecutor — those all live in krewhub now.

Flow:
    1. Read bundle_id (required) + prompt from the inbound A2A request.
    2. Discover online agents in the bundle's cookbook via krewhub.
    3. Generate pydantic-graph code via the injected code generator
       (defaults to llm_planner.generate_graph_code over direct A2A).
    4. POST the code to krewhub via KrewHubClient.attach_graph.
    5. Emit a completed A2A event with a small JSON artifact summarizing
       what was generated. Errors emit a failed status with a reason.

The old OrchestratorExecutor (in orchestrator_agent.py) is intentionally
left in place for the existing test_orchestrator_e2e test until that flow
is rewritten — the two coexist.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

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

from krewcli.client.krewhub_client import KrewHubClient
from krewcli.workflows.llm_planner import generate_graph_code

logger = logging.getLogger(__name__)


# A code generator takes (prompt, agents, agent_endpoints) and returns
# pydantic-graph source code as a string, or None on failure. Injectable
# so tests can replace it with a stub without going through the LLM.
CodeGenerator = Callable[
    [str, list[dict[str, Any]], dict[str, str]],
    Awaitable[str | None],
]


async def _default_code_generator(
    prompt: str,
    agents: list[dict[str, Any]],
    agent_endpoints: dict[str, str],
) -> str | None:
    """Default generator: direct A2A round trip via llm_planner.

    No krewhub task plumbing — krewhub already owns the target bundle
    and will receive the code via attach_graph. Creating a *second*
    bundle for codegen would race with the existing one.
    """
    return await generate_graph_code(
        prompt,
        agents,
        agent_endpoints,
        krewhub_client=None,
        recipe_id="",
    )


class PlannerOrchestratorExecutor(AgentExecutor):
    """A2A executor that generates graph code and POSTs it to krewhub.

    Required request metadata: ``bundle_id`` — the krewhub bundle the
    generated graph should attach to. Optional: ``cookbook_id`` to
    override the constructor default.
    """

    def __init__(
        self,
        *,
        krewhub_client: KrewHubClient,
        cookbook_id: str,
        code_generator: CodeGenerator | None = None,
    ) -> None:
        self._krewhub_client = krewhub_client
        self._cookbook_id = cookbook_id
        self._code_generator = code_generator or _default_code_generator

    async def execute(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        bundle_id = _extract_metadata(context, "bundle_id")
        cookbook_id = _extract_metadata(context, "cookbook_id") or self._cookbook_id
        prompt = _extract_text(context)

        if not bundle_id:
            await self._fail(
                event_queue, context,
                "missing required metadata: bundle_id",
            )
            return
        if not prompt:
            await self._fail(
                event_queue, context,
                "missing prompt text in message",
            )
            return

        await self._emit_status(
            event_queue, context, TaskState.working,
            f"Discovering agents in cookbook {cookbook_id}...",
        )

        try:
            agents = await self._krewhub_client.list_agents(cookbook_id)
        except Exception as exc:
            await self._fail(event_queue, context, f"agent discovery failed: {exc}")
            return

        agent_endpoints: dict[str, str] = {
            a["agent_id"]: a["endpoint_url"]
            for a in agents
            if a.get("endpoint_url") and a.get("status") != "offline"
        }
        if not agent_endpoints:
            await self._fail(
                event_queue, context,
                f"no online gateways in cookbook {cookbook_id}",
            )
            return

        await self._emit_status(
            event_queue, context, TaskState.working,
            f"Generating graph code via {len(agent_endpoints)} agent(s)...",
        )

        try:
            code = await self._code_generator(prompt, agents, agent_endpoints)
        except Exception as exc:
            await self._fail(event_queue, context, f"code generation crashed: {exc}")
            return

        if not code:
            await self._fail(
                event_queue, context,
                "code generator returned no output",
            )
            return

        await self._emit_status(
            event_queue, context, TaskState.working,
            f"Attaching graph code ({len(code)} bytes) to bundle {bundle_id}...",
        )

        try:
            attach_result = await self._krewhub_client.attach_graph(
                bundle_id, code, created_by="orchestrator",
            )
        except httpx.HTTPStatusError as exc:
            detail = _safe_error_detail(exc.response)
            await self._fail(
                event_queue, context,
                f"krewhub rejected attach_graph ({exc.response.status_code}): {detail}",
            )
            return
        except httpx.RequestError as exc:
            await self._fail(
                event_queue, context,
                f"krewhub unreachable for attach_graph: {exc}",
            )
            return

        result_summary = {
            "bundle_id": bundle_id,
            "code_bytes": len(code),
            "task_count": len(attach_result.get("tasks", [])),
            "node_ids": [
                t.get("graph_node_id")
                for t in attach_result.get("tasks", [])
                if t.get("graph_node_id")
            ],
            "mermaid": attach_result.get("bundle", {}).get("graph_mermaid", ""),
        }

        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                artifact=new_text_artifact(
                    name="graph_attached",
                    text=json.dumps(result_summary, indent=2),
                ),
            )
        )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                final=True,
                status=TaskStatus(state=TaskState.completed),
            )
        )
        logger.info(
            "planner: bundle %s — generated %d-byte graph, %d tasks created",
            bundle_id, len(code), result_summary["task_count"],
        )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                final=True,
                status=TaskStatus(state=TaskState.canceled),
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _emit_status(
        self, event_queue, context, state: TaskState, message_text: str,
    ) -> None:
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                final=False,
                status=TaskStatus(
                    state=state,
                    message=new_agent_text_message(message_text),
                ),
            )
        )

    async def _fail(
        self, event_queue, context, reason: str,
    ) -> None:
        logger.warning("planner: %s", reason)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                final=True,
                status=TaskStatus(
                    state=TaskState.failed,
                    message=new_agent_text_message(reason),
                ),
            )
        )


# ---------------------------------------------------------------------------
# Agent card
# ---------------------------------------------------------------------------


def build_planner_card(host: str, port: int) -> AgentCard:
    return AgentCard(
        name="planner",
        description=(
            "Generates a validated pydantic-graph workflow from a prompt and "
            "attaches it to a krewhub bundle for execution."
        ),
        url=f"http://{host}:{port}",
        version="0.1.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(transport="JSONRPC", url=f"http://{host}:{port}")
        ],
        skills=[
            AgentSkill(
                id="generate-graph",
                name="Generate Graph",
                description=(
                    "Decompose a prompt into a pydantic-graph workflow and "
                    "attach it to the bundle named in metadata.bundle_id."
                ),
                tags=["planner", "generate-graph", "decompose"],
                examples=[
                    "Add user authentication with login and signup",
                    "Refactor the database layer to use connection pooling",
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def _extract_text(context: RequestContext) -> str:
    if context.message and context.message.parts:
        for part in context.message.parts:
            if hasattr(part, "root") and hasattr(part.root, "text"):
                return part.root.text
            if hasattr(part, "text"):
                return part.text
    return ""


def _extract_metadata(context: RequestContext, key: str) -> str:
    if context.message and context.message.metadata:
        meta = context.message.metadata
        if isinstance(meta, dict):
            value = meta.get(key)
            if isinstance(value, str):
                return value
    return ""


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
    except (ValueError, AttributeError):
        pass
    return response.text[:200] if response.text else "no detail"
