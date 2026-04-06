from __future__ import annotations

import logging
from typing import Any

from pydantic_graph.beta import GraphBuilder, StepContext
from pydantic_graph.beta.join import reduce_list_append

from krewcli.workflows.agent_dispatch import (
    dispatch_to_agent,
    pick_available_agent,
    wait_for_task_completion,
)
from krewcli.workflows.orchestrator_state import (
    OrchestratorDeps,
    OrchestratorState,
    TaskNodeResult,
)

logger = logging.getLogger(__name__)


async def _dispatch_and_wait(
    ctx: StepContext[OrchestratorState, OrchestratorDeps, Any],
    node_id: str,
) -> str:
    """Helper injected into LLM-generated code namespace.

    Each graph step calls this to:
    1. Look up the krewhub task_id for this node_id
    2. Pick an available agent
    3. Dispatch via A2A JSON-RPC message/send
    4. Poll krewhub until done/blocked
    5. Record result in ctx.state.task_results
    """
    deps = ctx.deps
    task_id = deps.task_id_map.get(node_id, "")
    if not task_id:
        logger.error("No krewhub task_id mapped for node %s", node_id)
        return f"error: no task mapping for {node_id}"

    agent_id, endpoint_url = pick_available_agent(deps.agent_endpoints)

    prompt = f"[Task {node_id}] {ctx.state.prompt}"
    dispatched = await dispatch_to_agent(
        deps.a2a_client,
        endpoint_url,
        task_id,
        ctx.state.bundle_id,
        prompt,
        deps.recipe_meta,
    )

    if not dispatched:
        ctx.state.task_results[node_id] = TaskNodeResult(
            node_id=node_id,
            task_id=task_id,
            success=False,
            summary=f"Agent {agent_id} rejected task",
        )
        return f"error: dispatch rejected by {agent_id}"

    try:
        result = await wait_for_task_completion(
            deps.krewhub_client,
            task_id,
            poll_interval=deps.poll_interval,
            timeout=deps.task_timeout,
        )
    except TimeoutError:
        ctx.state.task_results[node_id] = TaskNodeResult(
            node_id=node_id,
            task_id=task_id,
            success=False,
            summary="Timed out waiting for completion",
        )
        return f"error: timeout for {node_id}"

    success = result.get("status") == "done"
    summary = result.get("blocked_reason", "") if not success else "completed"

    ctx.state.task_results[node_id] = TaskNodeResult(
        node_id=node_id, task_id=task_id, success=success, summary=summary
    )
    return f"{'done' if success else 'blocked'}: {summary}"


def execute_graph_code(code: str) -> Any:
    """Execute LLM-generated GraphBuilder Python code in a restricted namespace.

    The code is expected to define a ``graph`` variable (the built Graph).
    Returns the Graph object.

    Raises ValueError if the code doesn't define ``graph`` or fails to execute.
    """
    namespace: dict[str, Any] = {
        "GraphBuilder": GraphBuilder,
        "StepContext": StepContext,
        "reduce_list_append": reduce_list_append,
        "dispatch_and_wait": _dispatch_and_wait,
        "OrchestratorState": OrchestratorState,
        "OrchestratorDeps": OrchestratorDeps,
    }

    try:
        exec(code, namespace)  # noqa: S102
    except Exception as exc:
        raise ValueError(f"Graph code execution failed: {exc}") from exc

    graph = namespace.get("graph")
    if graph is None:
        raise ValueError("Graph code did not define a 'graph' variable")

    return graph


def extract_node_ids(graph: Any) -> list[str]:
    """Extract step node IDs from a built Graph.

    Skips internal nodes like __start__, __end__, forks, joins.
    """
    node_ids: list[str] = []

    # Beta GraphBuilder graphs use .nodes dict with Step objects
    if hasattr(graph, "nodes") and isinstance(graph.nodes, dict):
        for name, node in graph.nodes.items():
            if name.startswith("__"):
                continue
            # Skip join/fork internal nodes
            type_name = type(node).__name__
            if type_name in ("StartNode", "EndNode", "ForkNode", "JoinNode"):
                continue
            node_ids.append(name)
        if node_ids:
            return node_ids

    # Stable API (BaseNode graphs) use .node_defs
    if hasattr(graph, "node_defs"):
        for name in graph.node_defs:
            if name.startswith("__"):
                continue
            node_ids.append(name)
        return node_ids

    logger.warning("Cannot extract node IDs from graph type %s", type(graph))
    return node_ids


def build_fallback_graph(prompt: str) -> tuple[Any, list[str]]:
    """Build a simple 3-step graph as fallback when code generation fails.

    Returns (graph, node_ids).
    """
    g = GraphBuilder(
        state_type=OrchestratorState,
        deps_type=OrchestratorDeps,
        output_type=str,
    )

    @g.step
    async def scope(
        ctx: StepContext[OrchestratorState, OrchestratorDeps, None],
    ) -> str:
        return await _dispatch_and_wait(ctx, "scope")

    @g.step
    async def implement(
        ctx: StepContext[OrchestratorState, OrchestratorDeps, str],
    ) -> str:
        return await _dispatch_and_wait(ctx, "implement")

    @g.step
    async def review(
        ctx: StepContext[OrchestratorState, OrchestratorDeps, str],
    ) -> str:
        return await _dispatch_and_wait(ctx, "review")

    g.add(
        g.edge_from(g.start_node).to(scope),
        g.edge_from(scope).to(implement),
        g.edge_from(implement).to(review),
        g.edge_from(review).to(g.end_node),
    )

    graph = g.build()
    return graph, ["scope", "implement", "review"]
