"""Tier 3 Agent: Model-agnostic pydantic-graph orchestrator.

Receives a prompt, delegates graph code generation to any online A2A agent,
validates the graph (exec + node extraction + mermaid render), retries per
harness config, creates krewhub bundle/tasks, executes graph steps by
dispatching to online agents, and reports results.
"""

from __future__ import annotations

import json
import logging

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

from krewcli.agents.presets import orchestrator_preset
from krewcli.client.krewhub_client import KrewHubClient
from krewcli.workflows.executor import GraphExecutor
from krewcli.workflows.graph_builder import (
    build_fallback_graph,
    execute_graph_code,
    extract_node_ids,
)
from krewcli.workflows.graph_renderer import render_graph
from krewcli.workflows.llm_planner import generate_graph_code

logger = logging.getLogger(__name__)

_A2A_TIMEOUT = 30.0


class OrchestratorExecutor(AgentExecutor):
    """Model-agnostic orchestrator: delegates code generation to online A2A agents."""

    def __init__(
        self,
        krewhub_client: KrewHubClient | None = None,
        cookbook_id: str = "",
        **kwargs,
    ) -> None:
        self._krewhub_client = krewhub_client
        self._cookbook_id = cookbook_id

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        await _emit_status(
            event_queue, context, TaskState.working, "Discovering agents..."
        )

        prompt = _extract_text(context)
        recipe_id = ""
        recipe_meta: dict[str, str] = {}

        if context.message and context.message.metadata:
            meta = context.message.metadata
            recipe_id = meta.get("recipe_id", "") if isinstance(meta, dict) else ""
            recipe_meta = {
                k: meta.get(k, "") if isinstance(meta, dict) else ""
                for k in ("recipe_name", "repo_url", "branch")
            }

        try:
            # 1. Discover online agents
            agents: list[dict] = []
            agent_endpoints: dict[str, str] = {}
            if self._krewhub_client:
                agents = await self._krewhub_client.list_agents(
                    self._cookbook_id or None
                )
                agent_endpoints = {
                    a["agent_id"]: a["endpoint_url"]
                    for a in agents
                    if a.get("endpoint_url") and a.get("status") != "offline"
                }
            logger.info(
                "Orchestrator found %d agents: %s",
                len(agent_endpoints),
                list(agent_endpoints.keys()),
            )

            # 2-3. Generate graph code + validate, with retries per harness config
            harness = orchestrator_preset("").harness
            max_attempts = 1 + (harness.max_retries if harness else 0)

            graph = None
            node_ids: list[str] = []
            code: str | None = None

            for attempt in range(1, max_attempts + 1):
                await _emit_status(
                    event_queue,
                    context,
                    TaskState.working,
                    f"Requesting workflow graph (attempt {attempt}/{max_attempts})...",
                )

                try:
                    code = await generate_graph_code(
                        prompt, agents, agent_endpoints,
                        krewhub_client=self._krewhub_client,
                        recipe_id=recipe_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Code generation attempt %d failed: %s", attempt, exc
                    )
                    code = None

                if not code:
                    logger.warning(
                        "Attempt %d: no code returned by any agent", attempt
                    )
                    continue

                logger.info(
                    "Attempt %d: agent returned graph code:\n%s", attempt, code
                )

                try:
                    graph = execute_graph_code(code)
                    node_ids = extract_node_ids(graph)
                    if not node_ids:
                        raise ValueError("Graph has no step nodes")
                    rendered = render_graph(graph, direction="LR")
                    logger.info(
                        "Attempt %d: graph validated — %d nodes, %d edges, mermaid OK",
                        attempt,
                        rendered.node_count,
                        rendered.edge_count,
                    )
                    break
                except Exception as exc:
                    logger.warning(
                        "Attempt %d: validation failed: %s", attempt, exc
                    )
                    graph = None
                    node_ids = []

            if graph is None:
                logger.warning(
                    "All %d attempts failed, using fallback graph", max_attempts
                )
                graph, node_ids = build_fallback_graph(prompt)
                code = None

            logger.info("Graph has %d nodes: %s", len(node_ids), node_ids)

            # 4. Create krewhub bundle with tasks
            bundle_id = ""
            task_id_map: dict[str, str] = {}

            if self._krewhub_client and recipe_id:
                tasks_payload = [
                    {
                        "title": f"{node_id}: {prompt[:60]}",
                        "description": f"Step '{node_id}' of orchestrated workflow",
                        "depends_on_task_ids": [],
                    }
                    for node_id in node_ids
                ]

                # Set sequential dependencies for now
                # TODO: extract parallel structure from graph edges
                for i in range(1, len(tasks_payload)):
                    tasks_payload[i]["depends_on_task_ids"] = []

                bundle, created_tasks = await self._krewhub_client.create_bundle(
                    recipe_id=recipe_id,
                    prompt=prompt,
                    requested_by="orchestrator",
                    tasks=tasks_payload,
                )
                bundle_id = bundle["id"]

                for node_id, created_task in zip(node_ids, created_tasks):
                    task_id_map[node_id] = created_task["id"]

                logger.info(
                    "Created bundle %s with %d tasks", bundle_id, len(created_tasks)
                )

            # 5. Execute graph
            await _emit_status(
                event_queue, context, TaskState.working, "Executing workflow..."
            )

            async with httpx.AsyncClient(
                timeout=_A2A_TIMEOUT, follow_redirects=True
            ) as a2a_client:
                executor = GraphExecutor()
                exec_result = await executor.execute(
                    graph,
                    prompt=prompt,
                    recipe_id=recipe_id,
                    bundle_id=bundle_id,
                    krewhub_client=self._krewhub_client,
                    a2a_client=a2a_client,
                    task_id_map=task_id_map,
                    agent_endpoints=agent_endpoints,
                    recipe_meta=recipe_meta,
                )

            # 6. Report results
            result = {
                "workflow": "agent_generated" if code else "fallback",
                "bundle_id": bundle_id,
                "success": exec_result.success,
                "summary": exec_result.summary,
                "mermaid": exec_result.mermaid,
                "task_results": exec_result.task_results,
            }

            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=new_text_artifact(
                        name="result",
                        text=json.dumps(result, indent=2),
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

        except Exception as exc:
            logger.exception("Orchestrator failed")
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    final=True,
                    status=TaskStatus(
                        state=TaskState.failed,
                        message=new_agent_text_message(f"Orchestrator failed: {exc}"),
                    ),
                )
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                final=True,
                status=TaskStatus(state=TaskState.canceled),
            )
        )


async def _emit_status(event_queue, context, state, message_text):
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


def build_orchestrator_card(host: str, port: int) -> AgentCard:
    return AgentCard(
        name="orchestrator",
        description="Decomposes complex prompts into sub-tasks, dispatches to krewhub, "
        "monitors completion, and synthesizes results.",
        url=f"http://{host}:{port}",
        version="0.3.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(transport="JSONRPC", url=f"http://{host}:{port}")
        ],
        skills=[
            AgentSkill(
                id="orchestrate",
                name="Orchestrator",
                description="Decompose complex requests into sub-tasks and coordinate multi-agent execution.",
                tags=["orchestrate", "decompose", "coordinate", "synthesize"],
                examples=[
                    "Build an authentication system with login, signup, and password reset",
                    "Refactor the database layer to use connection pooling",
                ],
            )
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
